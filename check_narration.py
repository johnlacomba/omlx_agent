import sys
import os

# Add the current directory to sys.path to ensure omlx_agent can be imported
sys.path.append(os.getcwd())

try:
    from omlx_agent import _should_auto_nudge_for_narration
except ImportError as e:
    print(f"ImportError: {e}")
    sys.exit(1)

test_cases = [
    {
        "text": "The implementation is already complete and correct. All requirements have been fulfilled.",
        "active_ce_mode": "work",
        "round_num": 0,
        "expected": False
    },
    {
        "text": "Let me update the file now.",
        "active_ce_mode": "work",
        "round_num": 0,
        "expected": True
    },
    {
        "text": "You should add this block to the file.",
        "active_ce_mode": "work",
        "round_num": 0,
        "expected": True
    }
]

all_passed = True
for i, case in enumerate(test_cases):
    result = _should_auto_nudge_for_narration(case['text'], case['active_ce_mode'], case['round_num'])
    passed = result == case['expected']
    print(f"Test Case {i+1}: text='{case['text'][:30]}...', expected={case['expected']}, actual={result} -> {'PASS' if passed else 'FAIL'}")
    if not passed:
        all_passed = False

if all_passed:
    print("All tests passed.")
else:
    print("Some tests failed.")
    sys.exit(1)
