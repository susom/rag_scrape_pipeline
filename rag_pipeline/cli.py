import os
import sys

def list_urls():
    with open("config/urls.txt") as f:
        return [line.strip() for line in f if line.strip()]

def main():
    urls = list_urls()

    while True:
        print("\nAvailable URLs:")
        for i, url in enumerate(urls, 1):
            print(f"{i}. {url}")
        print("a. Run all")
        print("q. Quit")

        choice = input("\nEnter choice: ").strip().lower()

        if choice == "q":
            print("👋 Goodbye")
            break
        elif choice == "a":
            os.system("python -m rag_pipeline.main")
        elif choice.isdigit() and 1 <= int(choice) <= len(urls):
            idx = int(choice) - 1
            os.system(f"python -m rag_pipeline.main {urls[idx]}")
        else:
            print("❌ Invalid choice")

if __name__ == "__main__":
    main()
