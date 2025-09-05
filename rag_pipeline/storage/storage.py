import os

class StorageManager:
    def __init__(self, mode='local'):
        self.mode = mode
        if self.mode == 'local':
            self.base_path = 'cache'
            os.makedirs(self.base_path, exist_ok=True)
        else:
            # TODO: Implement Google Cloud Storage client setup here
            pass

    def save_file(self, filename, content):
        """Save a file locally (with nested dirs) or to GCS."""
        if self.mode == 'local':
            # Ensure full path exists
            path = (
                filename if filename.startswith(self.base_path)
                else os.path.join(self.base_path, filename)
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"Saved file: {path}")
        else:
            # TODO: Save file to GCS bucket
            pass
