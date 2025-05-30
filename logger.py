import os
from datetime import datetime, timedelta
import threading
import re

# --- Constants ---
MAX_RETENTION_LOG = 5  # Max retention time in minutes for local log file. Set to None for no limit.

class Logger:
    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = data_dir
        self.local_log_file = os.path.join(data_dir, f"logs_{port}.txt")
        self.global_log_file = "global_logs.txt"
        self.lock = threading.Lock()  # Thread-safe logging

        # Create log files if they don't exist
        for file in [self.local_log_file, self.global_log_file]:
            if not os.path.exists(file):
                with open(file, 'w') as f:
                    f.write('')

    def _get_timestamp(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _parse_log_timestamp(self, log_line):
        """Extract timestamp from a log line using regex."""
        match = re.match(r'^\[(.*?)\]', log_line)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return None

    def _write_log(self, file_path, message, prefix=""):
        """Write log entry, applying retention policy if configured for local log."""
        with self.lock:
            lines_to_write = None
            # Apply retention only for the local log file if MAX_RETENTION_LOG is set
            if file_path == self.local_log_file and MAX_RETENTION_LOG is not None:
                try:
                    if os.path.exists(file_path):
                        with open(file_path, 'r') as f:
                            lines = f.readlines()
                        
                        if len(lines) > 0:
                            first_timestamp = self._parse_log_timestamp(lines[0])
                            # Extract timestamp from the new message being added
                            new_timestamp = self._parse_log_timestamp(f"{prefix}{message}")

                            if first_timestamp and new_timestamp:
                                time_diff = new_timestamp - first_timestamp
                                time_diff_minutes = time_diff.total_seconds() / 60

                                # Check if retention period is exceeded
                                if time_diff_minutes > MAX_RETENTION_LOG:
                                    # Find the index of the first line within the retention period
                                    cutoff_time = new_timestamp - timedelta(minutes=MAX_RETENTION_LOG)
                                    start_index = 0
                                    for i, line in enumerate(lines):
                                        ts = self._parse_log_timestamp(line)
                                        if ts and ts >= cutoff_time:
                                            start_index = i
                                            break
                                        elif not ts:  # Handle lines without timestamp or parse error
                                            continue 
                                    # Keep lines from start_index onwards
                                    lines_to_write = lines[start_index:]
                                else:
                                    lines_to_write = lines  # No retention needed yet
                            else:
                                lines_to_write = lines  # Keep all if timestamps invalid
                        else:
                            lines_to_write = []  # Start fresh if file was empty
                    else:
                        lines_to_write = []  # File didn't exist

                    # Overwrite the file with retained lines if necessary
                    if lines_to_write is not None: 
                        with open(file_path, 'w') as f:
                            f.writelines(lines_to_write)

                except Exception as e:
                    print(f"Error during log retention check for {file_path}: {e}")
                    # Fallback to appending without retention if error occurs
                    lines_to_write = None 

            # Append the new message (either after overwrite or normally)
            try:
                with open(file_path, 'a') as f:
                    f.write(f"{prefix}{message}\n")
                    f.flush()  # Force the buffer to write to disk immediately
            except Exception as e:
                print(f"Error writing log to {file_path}: {e}")

    def info(self, message, global_log=False):
        timestamp = self._get_timestamp()
        local_entry = f"[{timestamp}][INFO] {message}"
        self._write_log(self.local_log_file, local_entry)

        if global_log:
            global_entry = f"[PORT {self.port}][{timestamp}][INFO] {message}"
            self._write_log(self.global_log_file, global_entry)

    def warn(self, message, global_log=False):
        timestamp = self._get_timestamp()
        local_entry = f"[{timestamp}][WARN] {message}"
        self._write_log(self.local_log_file, local_entry)

        if global_log:
            global_entry = f"[PORT {self.port}][{timestamp}][WARN] {message}"
            self._write_log(self.global_log_file, global_entry)

    def error(self, message, global_log=False):
        timestamp = self._get_timestamp()
        local_entry = f"[{timestamp}][ERROR] {message}"
        self._write_log(self.local_log_file, local_entry)

        if global_log:
            global_entry = f"[PORT {self.port}][{timestamp}][ERROR] {message}"
            self._write_log(self.global_log_file, global_entry)

    def snapshot_event_log(self, message):
        """
        Log snapshot creation events to both local instance log and snapshot_log.txt
        Uses the [INFO] prefix for consistency and single-line format
        """
        timestamp = self._get_timestamp()
        
        # Format message as a single line with INFO prefix
        formatted_message = f"[{timestamp}][INFO] {message}"
        
        # Write to local instance log (applies retention)
        self._write_log(self.local_log_file, formatted_message)
        
        # Write to snapshot log (no retention applied here)
        snapshot_log_file = os.path.join(os.path.dirname(self.local_log_file), "snapshot_log.txt")
        try:
            with self.lock:
                with open(snapshot_log_file, 'a') as f:
                    f.write(f"{formatted_message}\n")
                    f.flush()
        except Exception as e:
            print(f"Error writing to snapshot log {snapshot_log_file}: {e}")

    def cleanup_log(self, message):
        """
        Special logging for cleanup events.
        Writes concise, single-line logs with [CLEANUP] prefix.
        """
        timestamp = self._get_timestamp()
        
        # Ensure message is a single line by replacing newlines with pipe separator
        message = message.replace('\n', ' | ')
        
        # Create concise log entry with CLEANUP prefix
        log_entry = f"[{timestamp}][CLEANUP] {message}"
        
        # Write to local log (applies retention)
        self._write_log(self.local_log_file, log_entry)
        
        # Write to global log
        global_entry = f"[PORT {self.port}]{log_entry}"
        self._write_log(self.global_log_file, global_entry)
        
        # Write to snapshot logs if the message contains snapshot-related information
        if "snapshot" in message.lower():
            snapshot_log_file = os.path.join(os.path.dirname(self.local_log_file), "snapshot_log.txt")
            try:
                with self.lock:
                    with open(snapshot_log_file, 'a') as f:
                        f.write(f"{log_entry}\n")
                        f.flush()
            except Exception as e:
                print(f"Error writing cleanup log to snapshot log {snapshot_log_file}: {e}")

    def get_local_logs(self, last_n_lines=100):
        try:
            with self.lock:
                with open(self.local_log_file, 'r') as f:
                    lines = f.readlines()
                    return lines[-last_n_lines:]  # Return last N lines
        except Exception as e:
            return [f"Error reading logs: {str(e)}"]

    def get_global_logs(self, last_n_lines=100):
        try:
            with self.lock:
                with open(self.global_log_file, 'r') as f:
                    lines = f.readlines()
                    return lines[-last_n_lines:]  # Return last N lines
        except Exception as e:
            return [f"Error reading logs: {str(e)}"] 