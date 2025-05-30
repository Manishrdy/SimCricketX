import ast
import os
import builtins

PROJECT_ROOT = os.path.dirname(__file__)  # or hard-code your path

def gather_defs_and_calls(root):
    defs = set()
    calls = set()

    for dirpath, _, files in os.walk(root):
        for fname in files:
            if not fname.endswith('.py'): continue
            fpath = os.path.join(dirpath, fname)
            try:
                tree = ast.parse(open(fpath, encoding='utf-8').read(), filename=fpath)
            except SyntaxError:
                continue

            # collect definitions
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    # record method name (class methods will be picked up too)
                    defs.add(node.name)

            # collect all function/method calls
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        calls.add(func.id)
                    elif isinstance(func, ast.Attribute):
                        calls.add(func.attr)

    return defs, calls

if __name__ == '__main__':
    defs, calls = gather_defs_and_calls(PROJECT_ROOT)
    # anything defined but never called
    unused = sorted(defs - calls)
    # anything called that isn't defined and not a builtin
    builtins_set = set(dir(builtins))
    missing = sorted(calls - defs - builtins_set)

    print("🚩 Declared but never used:")
    for name in unused:
        print("   •", name)

    print("\n🚩 Called but not defined:")
    for name in missing:
        print("   •", name)
