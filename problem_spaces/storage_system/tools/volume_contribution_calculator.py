import json
from typing import Dict, Any, List

def calculate_volume_contribution(fault_analysis: Dict[str, Any], system_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate volume contribution based on fault type and system data.
    Returns updated fault_analysis with volume_contributions.
    """
    print("\n=== Volume Contribution Calculator Inputs ===")
    print(f"System Data: {json.dumps(system_data, indent=2)}")
    print(f"Fault Analysis Input: {json.dumps(fault_analysis, indent=2)}")

    fault_type = fault_analysis.get("fault_type", "No fault")
    details = fault_analysis.get("details", {})
    volumes = details.get("volume_details", [])

    # Convert max_capacity and max_throughput to integers, handle strings
    try:
        max_capacity = int(system_data.get("max_capacity", 1))
        max_throughput = int(system_data.get("max_throughput", 1))
    except (ValueError, TypeError) as e:
        print(f"Error: Invalid max_capacity or max_throughput values: {e}")
        fault_analysis["details"]["volume_contributions"] = []
        print("Volume Contributions Output: []")
        return fault_analysis

    volume_contributions = []

    if not volumes:
        print("Warning: No volume details available in fault_analysis")
        fault_analysis["details"]["volume_contributions"] = []
        print("Volume Contributions Output: []")
        return fault_analysis

    if fault_type == "High latency due to replication link issues":
        replication_issues = details.get("replication_issues", [])
        if replication_issues:
            primary_volume = replication_issues[0].get("volume_id", "")
            for volume in volumes:
                contribution = 100.0 if volume.get("volume_id") == primary_volume else 0.0
                volume_contributions.append({
                    "volume_id": volume.get("volume_id"),
                    "name": volume.get("name"),
                    "contribution_percentage": contribution,
                    "saturation_contribution": 0.0
                })
        else:
            print("Warning: Replication issues expected but not found")
            fault_analysis["details"]["volume_contributions"] = []
            print("Volume Contributions Output: []")
            return fault_analysis
    else:
        for volume in volumes:
            volume_size = volume.get("size", 0)
            snapshot_count = volume.get("snapshot_count", 0)
            snapshot_contribution = snapshot_count * volume_size  
            capacity_contribution = ((volume_size + snapshot_contribution) * 100) / max_capacity if max_capacity else 0.0

            saturation_contribution = 0.0
            if fault_type == "High latency due to high saturation":
                volume_throughput = volume.get("throughput", 0)
                saturation_contribution = (volume_throughput / max_throughput * 100) if max_throughput else 0.0

            volume_contributions.append({
                "volume_id": volume.get("volume_id"),
                "name": volume.get("name"),
                "contribution_percentage": round(capacity_contribution, 2),
                "saturation_contribution": round(saturation_contribution, 2)
            })

    volume_contributions = sorted(volume_contributions, key=lambda x: x["saturation_contribution" if fault_type == "High latency due to high saturation" else "contribution_percentage"], reverse=True)
    fault_analysis["details"]["volume_contributions"] = volume_contributions

    print("\n=== Volume Contribution Calculator Output ===")
    print(f"Volume Contributions: {json.dumps(volume_contributions, indent=2)}")
    return fault_analysis