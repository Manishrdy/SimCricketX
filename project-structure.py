#!/usr/bin/env python3
"""
Project Structure Printer
A script to display the entire directory structure of a project in tree format.
"""

import os
import argparse
from pathlib import Path

# Common directories/files to exclude by default
DEFAULT_EXCLUDES = {
    '.git', '.gitignore', '__pycache__', '.pytest_cache', 
    'node_modules', '.env', '.venv', 'venv', 'env',
    '.DS_Store', 'Thumbs.db', '.idea', '.vscode',
    '*.pyc', '*.pyo', '*.pyd', '.mypy_cache',
    'dist', 'build', '*.egg-info', '.coverage'
}

def should_exclude(path, excludes):
    """Check if a path should be excluded based on exclude patterns."""
    name = path.name
    
    # Check exact matches
    if name in excludes:
        return True
    
    # Check wildcard patterns
    for pattern in excludes:
        if '*' in pattern:
            if pattern.startswith('*') and name.endswith(pattern[1:]):
                return True
            elif pattern.endswith('*') and name.startswith(pattern[:-1]):
                return True
    
    return False

def print_tree(directory, prefix="", excludes=None, max_depth=None, current_depth=0):
    """
    Print directory structure in tree format.
    
    Args:
        directory: Path object of directory to print
        prefix: String prefix for tree formatting
        excludes: Set of patterns to exclude
        max_depth: Maximum depth to traverse (None for unlimited)
        current_depth: Current recursion depth
    """
    if excludes is None:
        excludes = DEFAULT_EXCLUDES
    
    if max_depth is not None and current_depth >= max_depth:
        return
    
    try:
        # Get all items in directory
        items = list(directory.iterdir())
        # Filter out excluded items
        items = [item for item in items if not should_exclude(item, excludes)]
        # Sort: directories first, then files, both alphabetically
        items.sort(key=lambda x: (x.is_file(), x.name.lower()))
        
        for i, item in enumerate(items):
            is_last = i == len(items) - 1
            
            # Choose the appropriate tree characters
            if is_last:
                current_prefix = "└── "
                next_prefix = prefix + "    "
            else:
                current_prefix = "├── "
                next_prefix = prefix + "│   "
            
            # Print current item
            print(f"{prefix}{current_prefix}{item.name}")
            
            # If it's a directory, recurse into it
            if item.is_dir():
                print_tree(item, next_prefix, excludes, max_depth, current_depth + 1)
                
    except PermissionError:
        print(f"{prefix}[Permission Denied]")

def get_project_info(root_path):
    """Get basic information about the project."""
    info = {
        'total_files': 0,
        'total_dirs': 0,
        'file_types': {}
    }
    
    for root, dirs, files in os.walk(root_path):
        # Filter out excluded directories
        dirs[:] = [d for d in dirs if not should_exclude(Path(d), DEFAULT_EXCLUDES)]
        
        info['total_dirs'] += len(dirs)
        info['total_files'] += len(files)
        
        # Count file extensions
        for file in files:
            if not should_exclude(Path(file), DEFAULT_EXCLUDES):
                ext = Path(file).suffix.lower()
                if ext:
                    info['file_types'][ext] = info['file_types'].get(ext, 0) + 1
                else:
                    info['file_types']['[no extension]'] = info['file_types'].get('[no extension]', 0) + 1
    
    return info

def main():
    parser = argparse.ArgumentParser(description='Print project directory structure')
    parser.add_argument('path', nargs='?', default='.', 
                       help='Root directory to analyze (default: current directory)')
    parser.add_argument('--max-depth', '-d', type=int, 
                       help='Maximum depth to traverse')
    parser.add_argument('--include-hidden', '-a', action='store_true',
                       help='Include hidden files and directories')
    parser.add_argument('--no-info', action='store_true',
                       help='Skip project statistics')
    parser.add_argument('--exclude', action='append', default=[],
                       help='Additional patterns to exclude (can be used multiple times)')
    
    args = parser.parse_args()
    
    # Convert path to Path object
    root_path = Path(args.path).resolve()
    
    if not root_path.exists():
        print(f"Error: Path '{args.path}' does not exist")
        return 1
    
    if not root_path.is_dir():
        print(f"Error: Path '{args.path}' is not a directory")
        return 1
    
    # Setup excludes
    excludes = DEFAULT_EXCLUDES.copy()
    excludes.update(args.exclude)
    
    # If including hidden files, remove common hidden file patterns
    if args.include_hidden:
        excludes = {e for e in excludes if not e.startswith('.')}
    
    print(f"📁 Project Structure: {root_path.name}")
    print(f"📍 Path: {root_path}")
    print("=" * 50)
    
    # Print the tree structure
    print_tree(root_path, excludes=excludes, max_depth=args.max_depth)
    
    # Print project statistics
    if not args.no_info:
        print("\n" + "=" * 50)
        print("📊 Project Statistics:")
        
        info = get_project_info(root_path)
        print(f"   Total directories: {info['total_dirs']}")
        print(f"   Total files: {info['total_files']}")
        
        if info['file_types']:
            print("\n   File types:")
            sorted_types = sorted(info['file_types'].items(), 
                                key=lambda x: x[1], reverse=True)
            for ext, count in sorted_types[:10]:  # Show top 10
                print(f"     {ext}: {count}")
            
            if len(sorted_types) > 10:
                print(f"     ... and {len(sorted_types) - 10} more")

if __name__ == "__main__":
    main()