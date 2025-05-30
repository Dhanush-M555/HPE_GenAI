import json
import os
import uuid
import threading
import time
import random
from datetime import datetime, timedelta
import requests

# --- Constants ---
MAX_RETENTION_METRICS = 3  # Max retention time in minutes for metrics files (system, replication, io). Set to None for no limit.

class StorageManager:
    def __init__(self, data_dir, global_file="global_systems.json", logger=None):
        self.data_dir = data_dir
        self.global_file = global_file
        self.logger = logger
        self.metrics_file = os.path.join(data_dir, f"system_metrics.json")
        self.replication_metrics_file = os.path.join(data_dir, f"replication_metrics.json")
        self.io_metrics_file = os.path.join(data_dir, "io_metrics.json") # Define io_metrics file path
        self.io_metrics_lock = threading.Lock()  # Lock for io_metrics.json
        self.replication_metrics_lock = threading.Lock()  # Lock for replication_metrics.json
        self.system_metrics_lock = threading.Lock()  # Lock for system_metrics.json
        os.makedirs(data_dir, exist_ok=True)

        self.snapshot_threads = {}

        self.cleanup_thread = None
        self.cleanup_stop = False
        # self.start_cleanup_thread() # Removed from here

        if not os.path.exists(self.global_file) or os.stat(self.global_file).st_size == 0:
            with open(self.global_file, "w") as f:
                json.dump([], f, indent=4)

        # Initialize metrics files if they don't exist
        self._initialize_metrics_file(self.metrics_file)
        self._initialize_metrics_file(self.replication_metrics_file)
        self._initialize_metrics_file(self.io_metrics_file)

        # Dictionary to keep track of ongoing replication tasks (one per volume)
        self.replication_tasks = {}
        
        # Dictionary to keep track of replication faults (source-target system links)
        self.replication_faults = {}

        # Add these class constants at the start of __init__
        self.IO_SIZE_KB = 8  # Default I/O size in KB
        self.FIXED_IOPS = 2000  # Fixed IOPS for all volumes
        self.IO_SIZE_OPTIONS = [4, 8, 16, 32, 64, 128]  # Valid I/O sizes in KB

    def _initialize_metrics_file(self, file_path):
        """Initialize a metrics file with an empty list if it doesn't exist."""
        if not os.path.exists(file_path):
            try:
                with open(file_path, 'w') as f:
                    json.dump([], f, indent=4)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Failed to initialize metrics file {file_path}: {str(e)}", global_log=True)

    def get_port(self):
        return self.data_dir.split('_')[-1]

    def _apply_retention_and_append(self, file_path, lock, new_entry, max_retention_minutes):
        """
        Helper function to load metrics, apply time-based retention, append new entry, and save.
        Handles concurrency using the provided lock.
        """
        with lock:
            try:
                # Read existing metrics
                if os.path.exists(file_path):
                    with open(file_path, 'r') as f:
                        metrics_list = json.load(f)
                    if not isinstance(metrics_list, list):
                        # Attempt to handle legacy format or reset
                        if isinstance(metrics_list, dict) and "timestamp" in metrics_list:
                             metrics_list = [metrics_list] # Convert single dict legacy format
                        else:
                             metrics_list = [] 
                else:
                    metrics_list = []

                # Apply retention policy
                if max_retention_minutes is not None and len(metrics_list) > 0:
                    try:
                        # Ensure the new entry has a timestamp
                        if "timestamp" not in new_entry:
                             new_entry["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                             
                        last_timestamp_str = new_entry["timestamp"]
                        first_timestamp_str = metrics_list[0].get("timestamp")

                        if first_timestamp_str and last_timestamp_str:
                            last_time = datetime.strptime(last_timestamp_str, "%Y-%m-%d %H:%M:%S")
                            first_time = datetime.strptime(first_timestamp_str, "%Y-%m-%d %H:%M:%S")
                            time_diff = last_time - first_time
                            time_diff_minutes = time_diff.total_seconds() / 60

                            # Remove oldest entries if retention period exceeded
                            while time_diff_minutes > max_retention_minutes and len(metrics_list) > 0:
                                metrics_list.pop(0) # Remove the oldest entry
                                # Recalculate time diff if list still has entries
                                if len(metrics_list) > 0:
                                     first_timestamp_str = metrics_list[0].get("timestamp")
                                     if first_timestamp_str:
                                          first_time = datetime.strptime(first_timestamp_str, "%Y-%m-%d %H:%M:%S")
                                          time_diff = last_time - first_time
                                          time_diff_minutes = time_diff.total_seconds() / 60
                                     else:
                                          break # Stop if no valid timestamp found
                                else:
                                     break # Stop if list is empty
                                     
                    except (ValueError, TypeError, KeyError) as e:
                         if self.logger:
                              self.logger.warn(f"Could not parse timestamps for retention check in {file_path}: {e}", global_log=True)
                    except Exception as e:
                         if self.logger:
                              self.logger.error(f"Error during retention check for {file_path}: {e}", global_log=True)

                # Append the new metrics entry
                metrics_list.append(new_entry)
                
                # Atomic write to prevent corruption
                tmp_file_path = file_path + ".tmp"
                with open(tmp_file_path, "w") as f:
                    json.dump(metrics_list, f, indent=4)
                
                os.replace(tmp_file_path, file_path)  # Replace atomically
            
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Failed to save metrics to {file_path}: {str(e)}", global_log=True)

    def save_metrics(self, metrics_data):
        """
        Safely save system metrics as timeseries data by appending the new entry.
        
        Args:
            metrics_data: Dictionary with metrics to append to the timeseries
        """
        # Ensure required fields exist (provide defaults if missing)
        required_keys = ["throughput_used", "capacity_used", "saturation", "cpu_usage"]
        for key in required_keys:
            if key not in metrics_data:
                metrics_data[key] = 0
                
        # Ensure timestamp exists
        if "timestamp" not in metrics_data:
            metrics_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Use the helper function to handle retention and saving
        self._apply_retention_and_append(self.metrics_file, self.system_metrics_lock, metrics_data, MAX_RETENTION_METRICS)

    def load_metrics(self):
        """
        Load the most recent system metrics from the timeseries.
        
        Returns:
            Dictionary with the most recent metrics, or default values if none exist
        """
        default_metrics = {
            "throughput_used": 0,
            "capacity_used": 0,
            "saturation": 0,
            "cpu_usage": 0,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        try:
            if not os.path.exists(self.metrics_file):
                return default_metrics
                
            with self.system_metrics_lock: # Use lock for reading to be safe
                with open(self.metrics_file, 'r') as f:
                    metrics_list = json.load(f)
            
                # Handle different formats
                if isinstance(metrics_list, list):
                    if not metrics_list:
                        return default_metrics
                    # Return the most recent entry
                    return metrics_list[-1]
                elif isinstance(metrics_list, dict):
                    # Handle legacy format (single dict instead of list)
                    if "timestamp" not in metrics_list:
                        metrics_list["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    return metrics_list
                else:
                    return default_metrics
                
        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to load system metrics: {str(e)}", global_log=True)
            return default_metrics
    
    def get_metrics_history(self, hours=24):
        """
        Get system metrics history for the specified time period.
        
        Args:
            hours: Number of hours to look back (default 24)
            
        Returns:
            List of metric entries from the specified time period
        """
        try:
            if not os.path.exists(self.metrics_file):
                return []
                
            with self.system_metrics_lock: # Use lock for reading
                 with open(self.metrics_file, 'r') as f:
                      metrics_list = json.load(f)
            
            if not isinstance(metrics_list, list):
                # Handle legacy dict format
                if isinstance(metrics_list, dict):
                     return [metrics_list] 
                return []
                
            # Calculate cutoff time
            cutoff_time = datetime.now() - timedelta(hours=hours)
            cutoff_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")
            
            # Filter metrics by timestamp
            return [m for m in metrics_list if m.get("timestamp", "") >= cutoff_str]
                
        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to get metrics history: {str(e)}", global_log=True)
            return []

    def update_capacity_used(self, size_gb):
        """
        Update capacity used by adding a new entry with the updated capacity.
        
        Args:
            size_gb: Size in GB to add to the current capacity used
            
        Returns:
            Updated capacity used value
        """
        current_metrics = self.load_metrics()
        current_capacity = current_metrics.get("capacity_used", 0)
        new_capacity = current_capacity + size_gb
        
        # Create new metrics entry with updated capacity and other current values
        new_metrics = current_metrics.copy() # Start with the last known state
        new_metrics["capacity_used"] = new_capacity
        new_metrics["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Save the new metrics entry using the helper
        self._apply_retention_and_append(self.metrics_file, self.system_metrics_lock, new_metrics, MAX_RETENTION_METRICS)
        
        return new_capacity

    def load_resource(self, resource_type):
        file_path = os.path.join(self.data_dir, f"{resource_type}.json")
        if not os.path.exists(file_path):
            with open(file_path, "w") as f:
                json.dump([], f, indent=4)
            return []

        try:
            with open(file_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return []

    def save_resource(self, resource_type, data):
        file_path = os.path.join(self.data_dir, f"{resource_type}.json")
        existing_data = self.load_resource(resource_type)

        if not isinstance(existing_data, list):
            print(f"Warning: {resource_type}.json is not a list. Resetting to an empty list.")
            existing_data = []
        if isinstance(data, dict):
            if any(item["id"] == data["id"] for item in existing_data):
                raise ValueError(f"{resource_type} with ID {data['id']} already exists.")

        existing_data.append(data)
        with open(file_path, "w") as f:
            json.dump(existing_data, f, indent=4)

    def add_system_to_global(self, system_id, system_name, port):
        with open(self.global_file, "r") as f:
            global_systems = json.load(f)

        if any(s["id"] == system_id for s in global_systems):
            return
        
        global_systems.append({"id": system_id, "name": system_name, "port": port})
        with open(self.global_file, "w") as f:
            json.dump(global_systems, f, indent=4)

    def get_all_systems(self):
        with open(self.global_file, "r") as f:
            return json.load(f)

    def update_resource(self, resource_type, resource_id, updated_data):
        file_path = os.path.join(self.data_dir, f"{resource_type}.json")
        existing_data = self.load_resource(resource_type)
        for i, item in enumerate(existing_data):
            if item["id"] == resource_id:
                existing_data[i] = updated_data
                break
        try:
            with open(file_path, "w") as f:
                json.dump(existing_data, f, indent=4)
        except Exception as e:
            raise Exception(f"Failed to update {resource_type}: {str(e)}")

    def delete_resource(self, resource_type, resource_id):
        """
        Delete a resource from its corresponding JSON file.
        """
        file_path = os.path.join(self.data_dir, f"{resource_type}.json")
        existing_data = self.load_resource(resource_type)
        
        # Simplified logging for snapshots
        if resource_type == "snapshots":
            # Only log the essential info in a single line
            self.logger.info(f"Deleted snapshot {resource_id}, current {resource_type} count: {len(existing_data)-1}", global_log=True)
        else:
            # For other resources, keep the original logging
            self.logger.info(f"Attempting to delete {resource_type} with ID: {resource_id}", global_log=True)
            self.logger.info(f"Current {resource_type} count before deletion: {len(existing_data)}", global_log=True)
            
            # Log the specific resource being deleted if not a snapshot
            resource_to_delete = next((item for item in existing_data if item["id"] == resource_id), None)
            if resource_to_delete:
                self.logger.info(f"Found {resource_type} to delete: {resource_to_delete}", global_log=True)
            else:
                self.logger.warn(f"No {resource_type} found with ID: {resource_id}", global_log=True)
            
        # Filter out the resource to delete
        existing_data = [item for item in existing_data if item["id"] != resource_id]
        
        # Verify deletion - only log errors, not success
        if any(item["id"] == resource_id for item in existing_data):
            self.logger.error(f"Failed to remove {resource_type} with ID: {resource_id}", global_log=True)
            raise Exception(f"Failed to delete {resource_type}: Resource still exists after deletion")
        
        try:
            with open(file_path, "w") as f:
                json.dump(existing_data, f, indent=4)
            
            # Skip final success logging for snapshots - already logged above
            if resource_type != "snapshots":
                self.logger.info(f"Successfully deleted {resource_type} with ID: {resource_id}", global_log=True)
                self.logger.info(f"Final {resource_type} count after deletion: {len(existing_data)}", global_log=True)
            
        except Exception as e:
            self.logger.error(f"Failed to delete {resource_type}: {str(e)}", global_log=True)
            raise Exception(f"Failed to delete {resource_type}: {str(e)}")
    
    def remove_system_from_global(self, system_id):
        """Removes a system from global_systems.json when deleted."""
        try:
            with open(self.global_file, "r") as f:
                global_systems = json.load(f)

            # Remove the system with the matching ID
            updated_systems = [sys for sys in global_systems if sys["id"] != system_id]

            with open(self.global_file, "w") as f:
                json.dump(updated_systems, f, indent=4)

            print(f"System {system_id} removed from global_systems.json")

        except Exception as e:
            raise Exception(f"Failed to remove system from global tracking: {str(e)}")
    def delete_related_resources(self, resource_type, system_id):
        """Deletes all resources (nodes, volumes, settings) associated with a system."""
        file_path = os.path.join(self.data_dir, f"{resource_type}.json")
        existing_data = self.load_resource(resource_type)

        # Keep only resources that DO NOT belong to the deleted system
        updated_data = [item for item in existing_data if item["system_id"] != system_id]

        try:
            with open(file_path, "w") as f:
                json.dump(updated_data, f, indent=4)

            print(f"All {resource_type} related to system {system_id} deleted.")

        except Exception as e:
            raise Exception(f"Failed to delete {resource_type} for system {system_id}: {str(e)}")
        
    def update_snapshot_in_settings(self, system_id, volume_id, snapshot_frequency):
        """Ensures snapshot settings for the volume are stored in settings.json."""
        file_path = os.path.join(self.data_dir, "settings.json")

        # If settings.json does not exist, create it
        if not os.path.exists(file_path):
            print("settings.json does not exist, creating a new file...")
            with open(file_path, "w") as f:
                json.dump([], f, indent=4)

        settings = self.load_resource("settings")

        # Find or create the system settings entry
        system_setting = next((s for s in settings if s["system_id"] == system_id), None)

        if not system_setting:
            print(f"No settings found for system {system_id}, creating a new entry.")
            system_setting = {
                "id": str(uuid.uuid4()),  # Generate a unique settings ID
                "system_id": system_id,
                "volume_snapshots": {}  # Initialize snapshot tracking
            }
            settings.append(system_setting)  # Add new settings entry

        # Update snapshot settings for the specific volume
        system_setting["volume_snapshots"][volume_id] = snapshot_frequency
        print(f"Updated settings: {settings}")

        # Save changes back to settings.json
        try:
            with open(file_path, "w") as f:
                json.dump(settings, f, indent=4)
            print(f"Snapshot settings updated successfully for volume {volume_id} in system {system_id}")

        except Exception as e:
            raise Exception(f"Failed to update snapshot settings in settings.json: {str(e)}")
    
    def update_replication_in_settings(self, system_id, replication_type, replication_target, replication_frequency):
        """Updates replication type and frequency in settings.json."""
        settings = self.load_resource("settings")

        # Find system settings entry
        system_setting = next((s for s in settings if s["system_id"] == system_id), None)

        if not system_setting:
            system_setting = {
                "id": str(uuid.uuid4()),
                "system_id": system_id,
                "replication_type": replication_type,
                "replication_target": replication_target
            }
            settings.append(system_setting)

        # Update replication type & target
        system_setting["replication_type"] = replication_type
        system_setting["replication_target"] = replication_target

        # Update frequency if async
        if replication_type == "asynchronous":
            system_setting["replication_frequency"] = replication_frequency
        else:
            system_setting.pop("replication_frequency", None)

        # Save changes
        try:
            file_path = os.path.join(self.data_dir, "settings.json")
            with open(file_path, "w") as f:
                json.dump(settings, f, indent=4)
        except Exception as e:
            raise Exception(f"Failed to update replication settings in settings.json: {str(e)}")
        

    def export_volume(self, volume_id, host_id, workload_size):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Exporting volume {volume_id} to host {host_id}")  # Debug log

        # Load volumes and hosts
        volumes = self.load_resource("volume")
        hosts = self.load_resource("host")

        # Find the volume and host
        volume = next((v for v in volumes if v["id"] == volume_id), None)
        host = next((h for h in hosts if h["id"] == host_id), None)

        if not volume or not host:
            raise ValueError("Invalid volume or host ID")

        # Check if volume is already exported
        if volume.get("is_exported"):
            raise ValueError("Volume is already exported")

        # Mark volume as exported
        volume["is_exported"] = True
        volume["exported_host_id"] = host_id
        volume["workload_size"] = workload_size

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting Host I/O for volume {volume_id}")

        # Use update_resource() instead of save_resource()
        self.update_resource("volume", volume_id, volume)  # Updates only this volume

        # Start background tasks: host I/O, snapshots, and replication.
        self.start_host_io(volume_id)
        if volume.get("snapshot_settings"):
            self.start_snapshot(volume_id, frequencies=volume["snapshot_settings"].get("frequencies", []))
        # If replication settings exist, start replication for this volume.
        if volume.get("replication_settings"):
            self.start_replication(volume_id)

        return f"Volume {volume_id} exported successfully to Host {host_id}"

    def start_host_io(self, volume_id):
        """Simulate I/O operations for a volume using logger"""
        print(f"Host I/O started for volume {volume_id}")

        def io_worker():
            try:
                # Initial metric write (if volume is exported)
                volumes = self.load_resource("volume")
                volume = next((v for v in volumes if v["id"] == volume_id), None)
                if volume and volume.get("is_exported", False):
                    host_id = volume.get("exported_host_id", "Unknown")
                    io_count = 2000
                    throughput = self.calculate_volume_throughput(volume)
                    latency = self.calculate_latency(self.load_metrics()) 
                    new_metric = {
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "volume_id": volume_id,
                            "host_id": host_id,
                            "io_count": io_count,
                            "latency": latency,
                            "throughput": throughput
                    }
                    # Save initial IO metric using the helper
                    self._apply_retention_and_append(self.io_metrics_file, self.io_metrics_lock, new_metric, MAX_RETENTION_METRICS)

                # Periodic metric writes
                while True:
                    time.sleep(30)
                    # Reload volume info in case it was unexported
                    volumes = self.load_resource("volume")
                    volume = next((v for v in volumes if v["id"] == volume_id), None)
                    if not volume or not volume.get("is_exported", False):
                        break

                    host_id = volume.get("exported_host_id", "Unknown")
                    io_count = 2000
                    latency = self.calculate_latency(self.load_metrics())
                    throughput = self.calculate_volume_throughput(volume)

                    new_metric = {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "volume_id": volume_id,
                        "host_id": host_id,
                        "io_count": io_count,
                        "latency": latency,
                        "throughput": throughput
                    }
                    
                    # Save periodic IO metric using the helper
                    self._apply_retention_and_append(self.io_metrics_file, self.io_metrics_lock, new_metric, MAX_RETENTION_METRICS)

                    if self.logger:
                        self.logger.info(
                            f"Volume: {volume_id}, "
                            f"Host: {host_id}, IOPS: {io_count}, Latency: {latency}ms, "
                            f"Throughput: {throughput} MB/s"
                        )

            except Exception as e:
                if self.logger:
                    self.logger.error(f"Host I/O error for volume {volume_id}: {str(e)}", global_log=True)

        worker_thread = threading.Thread(target=io_worker, daemon=True)
        worker_thread.start()
        print(f"Background thread started for volume {volume_id}")

    def unexport_volume(self, volume_id, reason="Manual unexport"):
        """
        Unexport a volume and cleanup all associated processes
        """
        volumes = self.load_resource("volume")
        volume = next((v for v in volumes if v["id"] == volume_id), None)
        if not volume:
            raise ValueError("Invalid volume ID")
        if not volume.get("is_exported", False):
            raise ValueError("Volume is not exported")

        # First cleanup all processes
        self.cleanup_volume_processes(volume_id, reason=reason)

        # Then update volume state
        volume["is_exported"] = False
        volume["exported_host_id"] = None
        volume["workload_size"] = None

        self.logger.info(f"Volume {volume_id} unexported: {reason}", global_log=True)
        self.update_resource("volume", volume_id, volume)
        return f"Volume {volume_id} unexported successfully"

    def start_snapshot(self, volume_id, frequencies):
        """Starts multiple snapshot processes for the same volume at different frequencies."""
        print(f"📌 start_snapshot() called for volume {volume_id} with frequencies {frequencies} seconds.")

        log_file_path = os.path.join(self.data_dir, "snapshot_log.txt")

        # Ensure log file exists
        if not os.path.exists(log_file_path):
            print("📂 Creating snapshot_log.txt file...")
            try:
                with open(log_file_path, "w") as f:
                    f.write("=== Snapshot Log Started ===\n")
                print("✅ snapshot_log.txt created successfully!")
            except Exception as e:
                print(f"❌ ERROR: Could not create snapshot_log.txt: {e}")

        def snapshot_worker(frequency):
            while True:
                volumes = self.load_resource("volume")
                volume = next((v for v in volumes if v["id"] == volume_id), None)

                if not volume:
                    print(f"⚠️ Volume {volume_id} not found. Stopping snapshot process for {frequency} sec interval.")
                    break

                # Initialize snapshot count if not set
                if "snapshot_count" not in volume:
                    volume["snapshot_count"] = 0

                # Increment snapshot count
                volume["snapshot_count"] += 1
                self.update_resource("volume", volume_id, volume)

                # Create a new snapshot entry with size information
                snapshot_id = str(uuid.uuid4())
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Find the corresponding snapshot setting ID for this frequency
                setting_id = None
                for sid, freq in volume.get("snapshot_settings", {}).items():
                    if freq == frequency:
                        setting_id = sid
                        break

                if setting_id:
                    snapshot = {
                        "id": snapshot_id,
                        "volume_id": volume_id,
                        "snapshot_setting_id": setting_id,
                        "created_at": timestamp,
                        "frequency": frequency,
                        "size": volume.get("size", 0)  # Add size information from parent volume
                    }
                    
                    # Save the snapshot to snapshots.json
                    self.save_resource("snapshots", snapshot)
                    
                    # Update system metrics to reflect new capacity
                    self.update_system_metrics()
                    
                    # Use logger.snapshot_event_log instead of manual logging
                    log_message = f"Snapshot {snapshot_id} taken for volume {volume_id}, frequency {frequency} sec, size {snapshot['size']} GB, total snapshots: {volume['snapshot_count']}"
                    self.logger.snapshot_event_log(log_message)
                    print(f"✅ Snapshot log updated: {log_message}")
                else:
                    # Use logger.snapshot_event_log for warning messages too
                    log_message = f"⚠️ No matching snapshot setting found for frequency {frequency} sec"
                    self.logger.snapshot_event_log(log_message)
                    print(f"⚠️ {log_message}")

                time.sleep(frequency)

        # Stop any existing snapshot threads for this volume
        if volume_id in self.snapshot_threads:
            print(f"🔄 Restarting snapshot process for volume {volume_id} with new frequencies: {frequencies} sec")
            for freq in self.snapshot_threads[volume_id]:
                self.snapshot_threads[volume_id][freq]["stop"] = True  # Signal all existing threads to stop
            time.sleep(1)  # Give them time to stop

        # Start new snapshot threads for each frequency
        self.snapshot_threads[volume_id] = {}
        for frequency in frequencies:
            stop_flag = {"stop": False}
            self.snapshot_threads[volume_id][frequency] = stop_flag
            snapshot_thread = threading.Thread(target=snapshot_worker, args=(frequency,), daemon=True)
            snapshot_thread.start()
            print(f"🚀 Snapshot process started for volume {volume_id} at {frequency} sec intervals.")

    def update_snapshot_in_settings(self, system_id, volume_id, snapshot_frequencies):
        """Ensures multiple snapshot settings for a volume are stored in settings.json."""
        file_path = os.path.join(self.data_dir, "settings.json")

        # Ensure settings.json exists
        if not os.path.exists(file_path):
            print("📂 settings.json does not exist, creating a new file...")
            with open(file_path, "w") as f:
                json.dump([], f, indent=4)

        settings = self.load_resource("settings")

        # Find or create the system settings entry
        system_setting = next((s for s in settings if s["system_id"] == system_id), None)

        if not system_setting:
            print(f"⚠️ No settings found for system {system_id}, creating a new entry.")
            system_setting = {
                "id": str(uuid.uuid4()),
                "system_id": system_id,
                "volume_snapshots": {}
            }
            settings.append(system_setting)

        # Update snapshot settings for the volume (store multiple frequencies)
        system_setting["volume_snapshots"][volume_id] = snapshot_frequencies

        # Save changes
        try:
            with open(file_path, "w") as f:
                json.dump(settings, f, indent=4)
            print(f"✅ Snapshot settings updated for volume {volume_id} in system {system_id} with frequencies {snapshot_frequencies}")

        except Exception as e:
            raise Exception(f"⚠️ Failed to update snapshot settings: {str(e)}")

    def start_replication(self, volume_id):
        """
        Starts a replication process for the given volume_id if replication settings exist.
        """
        # If a replication task for this volume is already running, do nothing.
        if volume_id in self.replication_tasks:
            return

        # Create an Event to signal termination of the replication thread.
        stop_event = threading.Event()
        self.replication_tasks[volume_id] = stop_event

        # Start main replication coordinator thread
        thread = threading.Thread(target=self.replication_coordinator, args=(volume_id, stop_event), daemon=True)
        thread.start()

    def replication_coordinator(self, volume_id, stop_event):
        """
        Coordinates replication to multiple targets, spawning a worker thread for each target.
        """
        worker_threads = {}  # Keep track of worker threads by target_id

        while not stop_event.is_set():
            # Reload volume to check current state
            volumes = self.load_resource("volume")
            volume = next((v for v in volumes if v["id"] == volume_id), None)

            if not volume or not volume.get("is_exported") or not volume.get("replication_settings"):
                break

            # Get current replication settings
            current_settings = volume.get("replication_settings", [])
            current_target_ids = {s.get("replication_target", {}).get("id") for s in current_settings 
                                 if s.get("replication_target", {}).get("id") is not None}

            # Stop threads for removed targets
            for target_id in list(worker_threads.keys()):
                if target_id not in current_target_ids:
                    worker_threads[target_id]["stop_event"].set()
                    worker_threads[target_id]["thread"].join(timeout=1)
                    del worker_threads[target_id]

            # Start new threads for new targets
            for rep_setting in current_settings:
                target_id = rep_setting.get("replication_target", {}).get("id")
                if target_id is not None and target_id not in worker_threads:
                    target_stop_event = threading.Event()
                    worker_thread = threading.Thread(
                        target=self.replication_worker,
                        args=(volume_id, target_stop_event, rep_setting),
                        daemon=True
                    )
                    worker_threads[target_id] = {
                        "thread": worker_thread,
                        "stop_event": target_stop_event
                    }
                    worker_thread.start()

            time.sleep(5)  # Check for changes every 5 seconds

        # Clean up all worker threads
        for worker in worker_threads.values():
            worker["stop_event"].set()
            worker["thread"].join(timeout=1)

        if volume_id in self.replication_tasks:
            del self.replication_tasks[volume_id]

    def replication_worker(self, volume_id, stop_event, rep_setting):
        """
        Worker function that simulates replication of a volume to a specific target.
        """
        replication_type = rep_setting.get("replication_type")
        target = rep_setting.get("replication_target", {})
        target_id = target.get("id")
        last_log_time = 0
        # Make sync replication logs more frequent
        SYNC_LOG_INTERVAL = 30  # Log every 30 seconds for sync replication (reduced from 200)

        # Get source volume and system info
        volumes = self.load_resource("volume")
        systems = self.load_resource("system")
        volume = next((v for v in volumes if v["id"] == volume_id), None)
        system = next((s for s in systems if s["id"] == volume.get("system_id")), None)
 
        if not volume or not system:
            self.logger.error(f"Source volume or system not found for replication", global_log=True)
            return

        # Log replication start
        start_log = (f"Started {replication_type} replication for volume {volume_id} "
                     f"to target {target.get('name')}")
        self.logger.info(start_log, global_log=True)

        while not stop_event.is_set():
            # Reload volumes to check current state.
            volumes = self.load_resource("volume")
            volume = next((v for v in volumes if v["id"] == volume_id), None)
            if not volume or not volume.get("is_exported"):
                break
            
            delay_sec = rep_setting.get("delay_sec", 0)

            # Simulate base replication throughput 
            io_count = random.randint(50, 500)
            replication_throughput = round(io_count / 2.0, 2)  # MB/s

            # Get current timestamp
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Base time for replication between 0.01-0.05ms
            base_time_ms = round(random.uniform(0.01, 0.05), 3)
            
            # Check for replication fault and calculate total time
            fault = self.get_replication_fault(target_id)
            total_time_ms = base_time_ms
            fault_sleep_ms = 0
            
            # Only apply fault to synchronous replication
            if fault and replication_type == "synchronous":
                fault_sleep_ms = fault.get("sleep_time", 0)
                total_time_ms = base_time_ms + fault_sleep_ms
                
                # Removed direct latency update here - we'll rely on update_system_metrics 
                # which is called in add_replication_fault and remove_replication_fault
            
            # Update replication metrics with the calculated time
            metrics = {
                "throughput": replication_throughput,
                "latency": total_time_ms,  # This is now our calculated time
                "io_count": io_count,
                "replication_type": replication_type,
                "timestamp": timestamp
            }
            self.update_replication_metrics(volume_id, target_id, metrics)

            current_time = time.time()
            should_log = (
                replication_type != "synchronous" or  # Always log async
                last_log_time == 0 or  # First log
                (current_time - last_log_time) >= SYNC_LOG_INTERVAL  # Periodic sync log
            )

            # Determine target endpoint by looking up the target system in global systems.
            try:
                global_systems = self.get_all_systems()
                target_sys = next((s for s in global_systems if s["id"] == target_id), None)
                if target_sys:
                    target_port = target_sys["port"]
                    target_url = f"http://localhost:{target_port}/replication-receive"
                    
                    # Send the calculated total_time_ms to the target system
                    payload = {
                        "volume_id": volume_id,
                        "replication_throughput": replication_throughput,
                        "sender": self.data_dir,
                        "timestamp": timestamp,
                        "replication_type": replication_type,
                        "should_log": should_log,
                        "latency": total_time_ms,  # Send the calculated time to target
                        "source_volume": {
                             "id": volume["id"],
                             "name": volume["name"],
                             "size": volume["size"],
                             "system_name": system["name"]
                         }
                    }
                    
                    # Now actually apply the sleep time to simulate the delay
                    # Base delay
                    time.sleep(base_time_ms / 1000.0)
                    
                    # Fault sleep time (only for sync replication)
                    if fault_sleep_ms > 0 and replication_type == "synchronous":
                        time.sleep(fault_sleep_ms / 1000.0)
                    
                    # Send the request
                    response = requests.post(target_url, json=payload, timeout=5)
                    
                    if response.status_code != 200:
                        self.logger.warn(f"Failed to deliver replication data to target {target.get('name')}: {response.text}", global_log=True)
                    elif should_log:
                        # Log sender replication event with time taken only
                        if replication_type == "synchronous":
                            sender_log = (f"Active synchronous replication for volume {volume_id} "
                                        f"to target {target.get('name')} (TimeTaken: {total_time_ms}ms)")
                        else:
                            sender_log = (f"Replicating volume {volume_id} "
                                        f"to target {target.get('name')} (TimeTaken: {total_time_ms}ms)")
                        if self.logger:
                            self.logger.info(sender_log, global_log=True)
                        last_log_time = current_time
                else:
                    self.logger.warn(f"Target system with id {target_id} not found", global_log=True)
            except Exception as ex:
                self.logger.error(f"Replication error for volume {volume_id}: {str(ex)}", global_log=True)

            # Wait based on replication type and delay setting
            wait_time = delay_sec if replication_type == "asynchronous" and delay_sec > 0 else 10
            time.sleep(wait_time)

        # Log replication stop
        stop_log = f"Stopped {replication_type} replication for volume {volume_id} to target {target.get('name')}"
        self.logger.info(stop_log, global_log=True)

    def cleanup_volume_processes(self, volume_id, reason="", notify_targets=True):
        """
        Cleanup all processes for a volume and notify targets if needed
        """
        try:
            volume = next((v for v in self.load_resource("volume") if v["id"] == volume_id), None)
            if not volume:
                return

            # Stop replication tasks if running
            if volume_id in self.replication_tasks:
                self.replication_tasks[volume_id].set()  # Signal thread to stop
                if volume.get("replication_settings") and notify_targets:
                    # Notify all targets about replication stop
                    for rep_setting in volume.get("replication_settings", []):
                        target = rep_setting.get("replication_target", {})
                        target_port = next((s["port"] for s in self.get_all_systems() 
                                         if s["id"] == target.get("id")), None)
                        if target_port:
                            try:
                                url = f"http://localhost:{target_port}/replication-stop"
                                requests.post(url, json={
                                    "volume_id": volume_id,
                                    "reason": reason,
                                    "sender": self.data_dir
                                }, timeout=5)
                            except Exception as e:
                                self.logger.error(f"Failed to notify target {target.get('name')}: {str(e)}", 
                                               global_log=True)

            # Log the cleanup
            self.logger.info(f"Stopped all processes for volume {volume_id}: {reason}", global_log=True)

        except Exception as e:
            self.logger.error(f"Error during cleanup for volume {volume_id}: {str(e)}", global_log=True)

    def save_replication_metrics(self, metrics):
        """
        Safely save replication metrics to file. Use this when completely replacing the file.
        """
        # Ensure metrics is a list for timeseries data
        if not isinstance(metrics, list):
            self.logger.warn("Converting replication metrics to list format", global_log=True)
            metrics = []
        
        # Atomic write to prevent corruption
        tmp_file = self.replication_metrics_file + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(metrics, f, indent=4)
        
        # Replace the file atomically
        os.replace(tmp_file, self.replication_metrics_file)

    def load_replication_metrics(self):
        """
        Load replication metrics from file as a list for timeseries data.
        """
        if not os.path.exists(self.replication_metrics_file):
            return []
            
        try:
            with open(self.replication_metrics_file, "r") as f:
                metrics = json.load(f)
                
            # Ensure it's a list
            if not isinstance(metrics, list):
                # Handle legacy format conversion
                if isinstance(metrics, dict):
                    # Convert old nested format to flat timeseries
                    flat_metrics = []
                    for volume_id, targets in metrics.items():
                        for target_id, metric in targets.items():
                            flat_metric = {
                                "volume_id": volume_id,
                                "target_system_id": target_id,
                                "timestamp": metric.get("timestamp", ""),
                                "throughput": metric.get("throughput", 0),
                                "latency": metric.get("latency", 0),
                                "io_count": metric.get("io_count", 0),
                                "replication_type": metric.get("replication_type", ""),
                            }
                            flat_metrics.append(flat_metric)
                    return flat_metrics
                else:
                    return []
                    
            return metrics
        except Exception as e:
            self.logger.error(f"Error loading replication metrics: {str(e)}", global_log=True)
            return []

    def update_replication_metrics(self, volume_id, target_id, metric_data):
        """
        Update replication metrics by adding a new entry to the timeseries.
        Applies retention policy.
        """
        # Create new metric entry in timeseries format
        new_metric = {
            "volume_id": volume_id,
            "target_system_id": target_id,
            "host_id": self._get_volume_host_id(volume_id),
            "timestamp": metric_data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "throughput": metric_data.get("throughput", 0),
            "latency": metric_data.get("latency", 0),
            "io_count": metric_data.get("io_count", 0),
            "replication_type": metric_data.get("replication_type", ""),
        }
        
        # Use the helper function to handle retention and saving
        self._apply_retention_and_append(self.replication_metrics_file, self.replication_metrics_lock, new_metric, MAX_RETENTION_METRICS)

    def _get_volume_host_id(self, volume_id):
        """Helper to get the host_id for a volume if it's exported"""
        try:
            volumes = self.load_resource("volume")
            volume = next((v for v in volumes if v["id"] == volume_id), None)
            if volume and volume.get("is_exported"):
                return volume.get("exported_host_id", "")
        except Exception:
            pass
        return ""

    def force_system_metrics_update_for_fault(self, target_system_id, action="added", sleep_time=None):
        """
        Force an immediate update of system metrics after a fault is added or removed.
        
        Args:
            target_system_id: ID of the target system 
            action: 'added' or 'removed' to indicate what happened
            sleep_time: The sleep time in ms that was added/removed (only needed for 'added')
        """
        # Immediately update system metrics to reflect the new fault state
        self.update_system_metrics()

    def add_replication_fault(self, target_system_id, sleep_time, duration=None):
        """
        Adds a fault to a replication link between this system and target system
        
        Args:
            target_system_id: ID of the target system
            sleep_time: Delay in milliseconds to add to replication operations
            duration: Duration in seconds for the fault (None means permanent)
        
        Returns:
            Dictionary with fault information
        """
        fault_id = str(uuid.uuid4())
        fault_info = {
            "id": fault_id,
            "target_system_id": target_system_id,
            "sleep_time": sleep_time,
            "created_at": datetime.now().timestamp(),
            "expires_at": datetime.now().timestamp() + duration if duration else None
        }
        
        self.replication_faults[target_system_id] = fault_info
        
        # Immediately update system metrics to reflect new fault
        self.force_system_metrics_update_for_fault(target_system_id, "added", sleep_time)
        
        return fault_info
    
    def remove_replication_fault(self, target_system_id):
        """
        Removes a fault from a replication link
        
        Args:
            target_system_id: ID of the target system
        
        Returns:
            True if fault was removed, False if no fault existed
        """
        if target_system_id in self.replication_faults:
            fault = self.replication_faults.pop(target_system_id)
            
            # Immediately update system metrics to reflect removed fault
            self.force_system_metrics_update_for_fault(target_system_id, "removed")
            
            return True
        return False
    
    def get_replication_fault(self, target_system_id):
        """
        Gets information about a fault in a replication link
        
        Args:
            target_system_id: ID of the target system
        
        Returns:
            Dictionary with fault information or None if no fault exists
        """
        fault = self.replication_faults.get(target_system_id)
        
        # Check if fault has expired
        if fault and fault.get("expires_at") and fault["expires_at"] < datetime.now().timestamp():
            self.remove_replication_fault(target_system_id)
            return None
            
        return fault
    
    def get_all_replication_faults(self):
        """
        Gets all active replication faults
        
        Returns:
            Dictionary of target_system_id -> fault_info
        """
        # Remove expired faults
        for target_id in list(self.replication_faults.keys()):
            fault = self.replication_faults[target_id]
            if fault.get("expires_at") and fault["expires_at"] < datetime.now().timestamp():
                self.remove_replication_fault(target_id)
                
        return self.replication_faults

    def calculate_latency(self, system_metrics):
        """
        Calculate latency based on system saturation and capacity usage percentages.
        Also adds any active replication fault sleep times to the latency.
        Returns latency in milliseconds.
        """
        # Get current saturation and capacity usage percentages
        saturation_pct = system_metrics.get("saturation", 0)
        
        # Calculate capacity usage percentage
        system = self.load_resource("system")[0]  # Get first system
        max_capacity = float(system.get("max_capacity", 1024))  # Default 1TB
        current_capacity = system_metrics.get("capacity_used", 0)
        capacity_pct = (current_capacity / max_capacity) * 100 if max_capacity > 0 else 0

        highest_pct = max(saturation_pct, capacity_pct)

        base_latency = 1.0  # default 1ms
        if highest_pct <= 70:
            base_latency = 1.0
        elif 70 < highest_pct <= 80:
            base_latency = 2.0
        elif 80 < highest_pct <= 90:
            base_latency = 3.0
        elif 90 < highest_pct <= 100:
            base_latency = 4.0
        else:
            base_latency = 5.0
        
        # Get current active faults - force a refresh to ensure we have the latest
        active_faults = self.get_all_replication_faults()
        total_fault_ms = 0.0
        
        # Add all active fault sleep times (directly in ms)
        for target_id, fault in active_faults.items():
            fault_sleep_ms = fault.get("sleep_time", 0)
            total_fault_ms += fault_sleep_ms
        
        # Calculate total latency by adding base latency (in ms) and fault times (in ms)
        total_latency = base_latency + total_fault_ms
        
        return total_latency

    def update_system_metrics(self):
        """
        Update system metrics including throughput, capacity, and saturation.
        Creates a new entry in the system metrics timeseries.
        """
        try:
            # Load current system and volumes
            system_resource = self.load_resource("system")
            if not system_resource: # Check if system exists
                 # Optionally log: self.logger.warn("Cannot update system metrics: No system found.")
                 return 
            system = system_resource[0]
            volumes = self.load_resource("volume")
            snapshots = self.load_resource("snapshots")
            
            # Get system limits
            max_throughput_mb = float(system.get("max_throughput", 200))
            max_capacity_gb = float(system.get("max_capacity", 1024))
            
            # Calculate total throughput from exported volumes
            total_throughput = 0
            for volume in volumes:
                if volume.get("is_exported"):
                    total_throughput += self.calculate_volume_throughput(volume)
            
            # Calculate total capacity usage (volumes + snapshots)
            volume_capacity = sum(float(v.get("size", 0)) for v in volumes)
            snapshot_capacity = sum(float(s.get("size", 0)) for s in snapshots)
            total_capacity = volume_capacity + snapshot_capacity
            
            # Calculate capacity percentage
            capacity_pct = (total_capacity / max_capacity_gb * 100) if max_capacity_gb > 0 else 0
            
            # Calculate saturation percentage
            saturation = (total_throughput / max_throughput_mb * 100) if max_throughput_mb > 0 else 0
            
            # Create new metrics entry with timestamp
            metrics_data = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "throughput_used": total_throughput,
                "capacity_used": total_capacity,
                "saturation": saturation,
                "cpu_usage": min(100, saturation),  # CPU usage correlates with saturation
                "volume_capacity": volume_capacity,
                "snapshot_capacity": snapshot_capacity,  # Track snapshot capacity separately
                "capacity_percentage": capacity_pct
            }
            
            # Calculate new latency based on updated metrics and active faults
            # Pass the newly calculated metrics_data to calculate_latency
            current_latency = self.calculate_latency(metrics_data) 
            metrics_data["current_latency"] = current_latency
            
            # Save the metrics as a new entry in the timeseries using the helper
            self._apply_retention_and_append(self.metrics_file, self.system_metrics_lock, metrics_data, MAX_RETENTION_METRICS)
            
            # Only log errors or significant changes instead of every update
            # self.logger.info(
            #    f"System metrics updated - "
            #    f"Throughput: {total_throughput:.2f} MB/s, "
            #    f"Volume Capacity: {volume_capacity:.2f} GB, "
            #    f"Snapshot Capacity: {snapshot_capacity:.2f} GB, "
            #    f"Total Capacity: {total_capacity:.2f} GB ({capacity_pct:.1f}%), "
            #    f"Saturation: {saturation:.2f}%, "
            #    f"Latency: {current_latency:.2f}ms",
            #    global_log=True
            # )

        except Exception as e:
            self.logger.error(f"Failed to update system metrics: {str(e)}", global_log=True)

    def calculate_volume_throughput(self, volume):
        """
        Calculate throughput for a volume based on IOPS and I/O size.
        Returns throughput in MB/s.
        """
        FIXED_IOPS = 2000  # Fixed IOPS for all volumes
        io_size_kb = volume.get("workload_size", 4)  # Default to 4KB if not specified
        
        # Convert KB to MB and calculate throughput
        throughput_mb = (FIXED_IOPS * io_size_kb) / 1024
        return throughput_mb

    def start_cleanup_thread(self):
        """Start the background cleanup thread."""
        def cleanup_worker():
            while not self.cleanup_stop:
                try:
                    self.cleanup()
                except Exception as e:
                    self.logger.error(f"Error in cleanup thread: {str(e)}", global_log=True)
                time.sleep(30)  # Run cleanup every 30 seconds

        self.cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        self.cleanup_thread.start()
        self.logger.info("Started background cleanup thread", global_log=True)

    def stop_cleanup_thread(self):
        """Stop the background cleanup thread."""
        self.cleanup_stop = True
        if self.cleanup_thread:
            self.cleanup_thread.join(timeout=1)
            self.logger.info("Stopped background cleanup thread", global_log=True)

    def cleanup(self):
        """
        Perform cleanup tasks:
        - Remove oldest snapshots if they exceed max_snapshots for each snapshot setting
        - Update system capacity after snapshot removal
        - Update system throughput, CPU usage, and saturation correctly
        """
        # Check if a system exists before proceeding
        system_data = self.load_resource("system")
        if not system_data:
            # self.logger.warn("No system found. Skipping cleanup.", global_log=True) # Optional: Log only if needed for debugging
            return # Don't log or proceed if no system exists

        try:
            # Load necessary data
            system = system_data[0]
            settings = self.load_resource("settings")
            settings_dict = {s["id"]: s for s in settings}

            # Track capacity changes
            initial_capacity = self.load_metrics().get("capacity_used", 0)
            capacity_freed = 0

            volumes = self.load_resource("volume")
            cleaned_snapshots = 0
            cleanup_summary = {}

            for volume in volumes:
                volume_id = volume["id"]
                snapshot_count = volume.get("snapshot_count", 0)
                volume_snapshot_settings = volume.get("snapshot_settings", {})

                cleanup_summary[volume_id] = {}

                for setting_id, frequency in volume_snapshot_settings.items():
                    setting = settings_dict.get(setting_id)
                    if not setting or setting["type"] != "snapshot":
                        continue
                    
                    max_snapshots = setting.get("max_snapshots", 10)
                    
                    # Fetch snapshots for this specific setting
                    snapshots = self.load_resource("snapshots")
                    snapshots_for_setting = [s for s in snapshots if s.get("snapshot_setting_id") == setting_id]
                    
                    num_snapshots_for_setting = len(snapshots_for_setting)
                    
                    # Only log if snapshots exceed max limit
                    if num_snapshots_for_setting > max_snapshots:
                        self.logger.info(
                            f"Volume {volume_id}, Setting {setting_id}: "
                            f"Current snapshots: {num_snapshots_for_setting}, Max allowed: {max_snapshots}", 
                            global_log=True
                        )
                        
                        excess_count = num_snapshots_for_setting - max_snapshots
                        self.logger.cleanup_log(
                            f"Volume {volume_id}, Setting {setting_id}: {excess_count} excess snapshots detected (max: {max_snapshots})"
                        )

                        # Sort snapshots by creation date (oldest first)
                        snapshots_for_setting.sort(key=lambda x: x["created_at"])

                        # Delete the excess snapshots
                        for i in range(excess_count):
                            if i < len(snapshots_for_setting):
                                snapshot_to_delete = snapshots_for_setting[i]
                                try:
                                    # Track capacity being freed
                                    snapshot_size = float(snapshot_to_delete.get("size", 0))
                                    capacity_freed += snapshot_size

                                    # Delete the snapshot
                                    self.delete_resource("snapshots", snapshot_to_delete["id"])
                                    cleaned_snapshots += 1
                                    
                                    if setting_id not in cleanup_summary[volume_id]:
                                        cleanup_summary[volume_id][setting_id] = 0
                                    cleanup_summary[volume_id][setting_id] += 1

                                    # Verify deletion - only log errors
                                    snapshots_after = self.load_resource("snapshots")
                                    if any(s["id"] == snapshot_to_delete["id"] for s in snapshots_after):
                                        self.logger.error(
                                            f"Failed to delete snapshot {snapshot_to_delete['id']}", 
                                            global_log=True
                                        )
                                except Exception as e:
                                    self.logger.error(
                                        f"Error deleting snapshot {snapshot_to_delete['id']}: {str(e)}", 
                                        global_log=True
                                    )

                        # Update the snapshot count in the volume
                        volume["snapshot_count"] = min(snapshot_count, max_snapshots)
                        self.update_resource("volume", volume_id, volume)

            # Skip individual summary logs for each volume/setting combination
            # We'll just have the final summary at the end

            # Update system metrics after cleanup
            current_metrics = self.load_metrics()
            current_latency = current_metrics.get("current_latency") 
            
            # Only update metrics but preserve any existing fault-related latency
            self.update_system_metrics()

            # Get the current metrics after update
            final_capacity = self.load_metrics().get("capacity_used", 0)
            
            # Create a single concise cleanup summary line
            if cleaned_snapshots > 0:
                self.logger.cleanup_log(
                    f"Cleanup complete: {cleaned_snapshots} snapshots removed, Initial: {initial_capacity:.2f} GB, Freed: {capacity_freed:.2f} GB, Final: {final_capacity:.2f} GB"
                )
            else:
                # Even more concise if no snapshots were removed
                self.logger.cleanup_log(
                    f"Cleanup complete: No snapshots needed removal, Capacity: {final_capacity:.2f} GB"
                )

        except Exception as e:
            self.logger.error(f"Housekeeping error: {str(e)}", global_log=True)

    def delete_volume(self, volume_id):
        """
        Delete a volume and all its associated snapshots.
        """
        try:
            # First, cleanup any running processes for the volume
            self.cleanup_volume_processes(volume_id, reason="Volume deletion")
            
            # Load and delete all snapshots associated with this volume
            snapshots = self.load_resource("snapshots")
            volume_snapshots = [s for s in snapshots if s["volume_id"] == volume_id]
            
            # Log the number of snapshots to be deleted
            self.logger.info(
                f"Deleting volume {volume_id} and its {len(volume_snapshots)} associated snapshots",
                global_log=True
            )
            
            # Delete each snapshot
            capacity_freed = 0
            for snapshot in volume_snapshots:
                try:
                    snapshot_size = float(snapshot.get("size", 0))
                    capacity_freed += snapshot_size
                    self.delete_resource("snapshots", snapshot["id"])
                    self.logger.info(
                        f"Deleted snapshot {snapshot['id']} for volume {volume_id} (freed {snapshot_size} GB)",
                        global_log=True
                    )
                except Exception as e:
                    self.logger.error(
                        f"Error deleting snapshot {snapshot['id']}: {str(e)}",
                        global_log=True
                    )

            # Now delete the volume itself
            self.delete_resource("volume", volume_id)
            
            # Update system metrics after deletion
            self.update_system_metrics()
            
            # Log the total capacity freed
            self.logger.info(
                f"Volume {volume_id} and its snapshots deleted successfully. "
                f"Total capacity freed: {capacity_freed:.2f} GB",
                global_log=True
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to delete volume {volume_id}: {str(e)}", global_log=True)
            raise

class Settings:
    def __init__(self, id, system_id):
        self.id = id
        self.system_id = system_id
        

    def to_dict(self):
        return {
            "id": self.id,
            "system_id": self.system_id,
            
        }