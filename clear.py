import os
import shutil
import argparse
import sys

def cleanup_directory(directory_path):
    if not os.path.exists(directory_path):
        print(f"Error: Directory '{directory_path}' does not exist.")
        return 0, 0, 0
        
    pycache_count = 0
    json_count = 0
    data_instance_count = 0
    
    print(f"Starting cleanup of directory: {directory_path}")
    print("Searching for __pycache__ folders, data_instance* folders, and .json files...")
    
    for root, dirs, files in os.walk(directory_path, topdown=True):
        dirs_to_remove = []
        
        if '__pycache__' in dirs:
            pycache_path = os.path.join(root, '__pycache__')
            try:
                shutil.rmtree(pycache_path)
                print(f"Deleted __pycache__ folder: {pycache_path}")
                pycache_count += 1
                dirs.remove('__pycache__')
            except Exception as e:
                print(f"Error deleting {pycache_path}: {e}")
        
        # Check for data_instance* directories
        for dir_name in dirs:
            if dir_name.startswith('data_instance'):
                data_instance_path = os.path.join(root, dir_name)
                try:
                    shutil.rmtree(data_instance_path)
                    print(f"Deleted data_instance folder: {data_instance_path}")
                    data_instance_count += 1
                    dirs_to_remove.append(dir_name)  # Mark for removal
                except Exception as e:
                    print(f"Error deleting {data_instance_path}: {e}")
        
        # Remove the marked directories from dirs list
        for dir_name in dirs_to_remove:
            dirs.remove(dir_name)
                
        # Check for .json files, excluding config.json, tools.json, and data_model.json
        for file in files:
            if file.endswith('.json'):
                if file in ['config.json', 'tools.json', 'data_model.json']:
                    print(f"Skipping protected JSON file: {os.path.join(root, file)}")
                    continue
                json_path = os.path.join(root, file)
                try:
                    os.remove(json_path)
                    print(f"Deleted JSON file: {json_path}")
                    json_count += 1
                except Exception as e:
                    print(f"Error deleting {json_path}: {e}")
    
    return pycache_count, json_count, data_instance_count

def main():
    parser = argparse.ArgumentParser(
        description='Clean up __pycache__, data_instance* folders and .json files from a directory.'
    )
    parser.add_argument(
        'directory', 
        nargs='?',
        default=os.getcwd(),
        help='Directory path to clean (defaults to current directory)'
    )
    
    args = parser.parse_args()
    
    directory_path = os.path.abspath(args.directory)
    
    print(f"This will delete all __pycache__ folders, data_instance* folders, and .json files in: {directory_path}")
    print("Note: config.json, tools.json, and data_model.json will be preserved.")
    confirmation = input("Are you sure you want to proceed? (y/n): ")
    
    if confirmation.lower() not in ['y', 'yes']:
        print("Operation canceled.")
        sys.exit(0)

    pycache_count, json_count, data_instance_count = cleanup_directory(directory_path)
    
    print("\nCleanup completed.")
    print(f"Removed {pycache_count} __pycache__ directories")
    print(f"Removed {data_instance_count} data_instance* directories")
    print(f"Deleted {json_count} .json files")

if __name__ == "__main__":
    main()