
import os

file_path = r"d:\SimCricketX\templates\match_setup.html"

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Define the broken parts and the fixed versions
# We use a very specific replacement key to be safe
broken_snippet = "const preselectHome = {{ preselect_home if preselect_home else 'null' }\n    };"
fixed_snippet = "const preselectHome = {{ preselect_home if preselect_home else 'null' }};"

# Also handle the variant seen in some cases if possible, but let's stick to replacing the exact block
# Better: Find the line and replace it using logic
lines = content.split('\n')
new_lines = []
skip_next = False

for i in range(len(lines)):
    if skip_next:
        skip_next = False
        continue
        
    line = lines[i]
    if "const preselectHome = {{ preselect_home if preselect_home else 'null' }" in line and "}};" not in line:
        # found the broken line
        print(f"Found broken Home line: {line}")
        new_lines.append("        const preselectHome = {{ preselect_home if preselect_home else 'null' }};")
        # Ensure we check if the next line is the dangling closing brace
        if i + 1 < len(lines) and lines[i+1].strip() == "};":
            print("Skipping dangling brace on next line")
            skip_next = True
    else:
        new_lines.append(line)

new_content = '\n'.join(new_lines)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("File patched successfully.")
