"""Remove duplicate backup_database functions from app.py"""

with open(r'd:\SimCricketX\app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find all lines with backup_database function definitions
backup_func_lines = []
for i, line in enumerate(lines):
    if 'def backup_database():' in line:
        backup_func_lines.append(i + 1)  # 1-indexed

print(f"Found backup_database function at lines: {backup_func_lines}")

# Keep the first occurrence (line 2582), remove the others
# Need to find the start and end of each duplicate block

# Strategy: Remove lines 2657-2734 (second duplicate) and 2737-2814 (third duplicate)
# But line numbers will shift after first deletion, so work backwards

# First, let's identify the blocks more precisely
blocks_to_remove = []

# Find second duplicate starting at line 2657
for i in range(2656, min(2800, len(lines))):
    if i < len(lines) and '# ===== Database Backup Endpoint =====' in lines[i]:
        # Find the end of this block (next @app.route)
        end_line = i + 1
        for j in range(i + 1, min(i + 100, len(lines))):
            if '@app.route' in lines[j] and 'backup' not in lines[j]:
                end_line = j
                break
        blocks_to_remove.append((i, end_line))
        print(f"Block to remove: lines {i+1}-{end_line}")
        break

# Find third duplicate
for i in range(2736, min(2850, len(lines))):
    if i < len(lines) and 'ADMIN: Database Backup Endpoint' in lines[i]:
        end_line = i + 1
        for j in range(i + 1, min(i + 100, len(lines))):
            if '@app.route' in lines[j] and 'backup' not in lines[j]:
                end_line = j
                break
        blocks_to_remove.append((i, end_line))
        print(f"Block to remove: lines {i+1}-{end_line}")
        break

# Remove blocks in reverse order to preserve line numbers
blocks_to_remove.sort(reverse=True)

for start, end in blocks_to_remove:
    print(f"Removing lines {start+1} to {end}")
    del lines[start:end]

# Write back
with open(r'd:\SimCricketX\app.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print("Duplicates removed successfully!")
