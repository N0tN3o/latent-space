import os
from pathlib import Path
from datetime import datetime

# Automatically find the correct project paths relative to this script
# Assumes script is located at: /home/art/LatentSpace/admin_ui/delete_prints.py
CURRENT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = CURRENT_DIR.parent
PRINTS_DIR = PROJECT_ROOT / "shared_output" / "docs"

def manual_deletion():
    """Delete ALL PDF prints in the directory"""
    if not PRINTS_DIR.exists():
        print(f"Error: Directory not found: {PRINTS_DIR}")
        return

    print(f"⚠️  Deleting ALL prints from: {PRINTS_DIR}")
    count = 0
    for pdf_file in PRINTS_DIR.glob("print_*.pdf"):
        try:
            pdf_file.unlink()
            print(f"Deleted: {pdf_file.name}")
            count += 1
        except Exception as e:
            print(f"Failed to delete {pdf_file.name}: {e}")
    print(f"Total deleted: {count}")

def automatic_deletion():
    """Delete only prints older than TODAY"""
    if not PRINTS_DIR.exists():
        return

    # Get today's date as integer YYYYMMDD (e.g. 20251120)
    today_str = datetime.now().strftime("%Y%m%d")
    today_int = int(today_str)
    
    print(f"Running auto-deletion for files older than {today_str}...")
    
    count = 0
    for pdf_file in PRINTS_DIR.glob("print_*.pdf"):
        # Expected format: print_20251120_123456.pdf
        try:
            # Split filename to get the date part
            parts = pdf_file.name.split('_')
            if len(parts) >= 2 and parts[1].isdigit():
                file_date = int(parts[1])
                
                if file_date < today_int:
                    pdf_file.unlink()
                    print(f"Auto-deleted old file: {pdf_file.name}")
                    count += 1
        except Exception as e:
            print(f"Error processing {pdf_file.name}: {e}")
            
    print(f"Auto-deletion complete. Removed {count} files.")

if __name__ == "__main__":
    # Simple CLI menu for testing
    print("--- Print Deletion Tool ---")
    print(f"Target: {PRINTS_DIR}")
    print("1. Delete ALL prints")
    print("2. Delete OLD prints (older than today)")
    choice = input("Select option (1/2): ")
    
    if choice == "1":
        manual_deletion()
    elif choice == "2":
        automatic_deletion()
    else:
        print("Invalid choice")