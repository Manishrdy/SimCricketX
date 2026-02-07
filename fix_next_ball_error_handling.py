"""
Script to add error handling to the next_ball endpoint in app.py
Fixes the 500 error by ensuring all exceptions are logged and returned as JSON
"""

import re

# Read the app.py file
with open(r'd:\SimCricketX\app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the next_ball function and add try-except
old_pattern = r'''(    @app\.route\("/match/<match_id>/next-ball", methods=\["POST"\]\)
    @login_required
    @rate_limit\(max_requests=30, window_seconds=10\)  # C3: Rate limit to prevent DoS
    def next_ball\(match_id\):
        )(with MATCH_INSTANCES_LOCK:.*?
        return jsonify\(outcome\))'''

new_code = r'''\1try:
            \2
        except Exception as e:
            # Log the complete error with stack trace to execution.log
            app.logger.error(f"[NextBall] Error processing ball for match {match_id}: {e}", exc_info=True)
            
            # Also log to console for immediate visibility
            import traceback
            traceback.print_exc()
            
            # Return JSON error response instead of HTML 500 page
            return jsonify({
                "error": "An error occurred while processing the ball",
                "details": str(e),
                "match_id": match_id
            }), 500'''

# Apply the fix
content_fixed = re.sub(old_pattern, new_code, content, flags=re.DOTALL)

if content != content_fixed:
    # Write the fixed content back
    with open(r'd:\SimCricketX\app.py', 'w', encoding='utf-8') as f:
        f.write(content_fixed)
    print("✅ Successfully added error handling to next_ball endpoint")
else:
    print("⚠️ Pattern not found - applying manual fix")
    # Manual fix: find the function and insert try-except
    lines = content.split('\n')
    fixed_lines = []
    in_next_ball = False
    indent_added = False
    
    for i, line in enumerate(lines):
        if 'def next_ball(match_id):' in line and i > 0 and '@app.route("/match/<match_id>/next-ball"' in lines[i-2]:
            in_next_ball = True
            fixed_lines.append(line)
            # Add try block after function definition
            fixed_lines.append('        try:')
            indent_added = True
            continue
        
        if in_next_ball and indent_added:
            if line.strip().startswith('@app.route'):
                # End of function - add except block before this line
                fixed_lines.append('        except Exception as e:')
                fixed_lines.append('            # Log the complete error with stack trace to execution.log')
                fixed_lines.append('            app.logger.error(f"[NextBall] Error processing ball for match {match_id}: {e}", exc_info=True)')
                fixed_lines.append('            ')
                fixed_lines.append('            # Also log to console for immediate visibility')
                fixed_lines.append('            import traceback')
                fixed_lines.append('            traceback.print_exc()')
                fixed_lines.append('            ')
                fixed_lines.append('            # Return JSON error response instead of HTML 500 page')
                fixed_lines.append('            return jsonify({')
                fixed_lines.append('                "error": "An error occurred while processing the ball",')
                fixed_lines.append('                "details": str(e),')
                fixed_lines.append('                "match_id": match_id')
                fixed_lines.append('            }), 500')
                fixed_lines.append('        ')
                in_next_ball = False
                indent_added = False
                fixed_lines.append(line)
            elif line.startswith('    ') and not line.startswith('        '):
                # Don't indent decorators
                fixed_lines.append(line)
            else:
                # Indent function content
                fixed_lines.append('    ' + line if line.strip() else line)
        else:
            fixed_lines.append(line)
    
    if indent_added:  # We made changes
        with open(r'd:\SimCricketX\app.py', 'w', encoding='utf-8') as f:
            f.write('\n'.join(fixed_lines))
        print("✅ Successfully added error handling to next_ball endpoint (manual)")
    else:
        print("❌ Could not find next_ball function to fix")

print("\nNow errors will be:")
print("  1. Logged to logs/execution.log with full stack trace")
print("  2. Printed to console for immediate visibility") 
print("  3. Returned as JSON (not HTML) to frontend")
