import os
from datetime import datetime
from google.cloud import storage
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

class StorageManager:
    def __init__(self, mode: str = "local"):
        # keep the arg so main() doesn't break
        self.mode = mode.lower().strip() if mode else "local"
        self.base_path = "cache"
        os.makedirs(self.base_path, exist_ok=True)

        self.bucket_name = os.getenv("GCS_BUCKET", "").strip()
        self.client = None
        self.bucket = None

        if self.bucket_name:
            try:
                self.client = storage.Client()
                self.bucket = self.client.bucket(self.bucket_name)
                logger.info(f"GCS enabled → bucket: {self.bucket_name}")
            except Exception as e:
                logger.error(f"Could not init GCS client: {e}")
        else:
            logger.info("GCS disabled (no GCS_BUCKET). Running local-only.")

    # ---------- ALWAYS write locally ----------
    def save_file(self, filename: str, content: str):
        path = filename if filename.startswith(self.base_path) else os.path.join(self.base_path, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Saved locally: {path}")

    # ---------- MIRROR local cache → GCS ----------
    def upload_artifacts(self):
        if not self.bucket:
            logger.info("Skipping GCS upload — bucket not configured.")
            return

        if not os.path.isdir(self.base_path):
            logger.warning(f"Nothing to upload — '{self.base_path}' does not exist.")
            return

        run_id = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
        prefix = f"cache/{run_id}"
        to_upload = []

        for root, _, files in os.walk(self.base_path):
            for fname in files:
                if fname == ".DS_Store":
                    continue
                local_path = os.path.join(root, fname)
                rel_path = os.path.relpath(local_path, self.base_path)
                remote_path = f"{prefix}/{rel_path}".replace("\\", "/")  # windows-proof
                to_upload.append((local_path, remote_path))

        logger.info(f"Uploading {len(to_upload)} file(s) to gs://{self.bucket_name}/{prefix}/")

        uploaded = 0
        for local_path, remote_path in to_upload:
            logger.info(f"Attempting upload: {local_path} → gs://{self.bucket_name}/{remote_path}")

            try:
                blob = self.bucket.blob(remote_path)
                blob.upload_from_filename(local_path)
                uploaded += 1
                logger.info(f"Uploaded → gs://{self.bucket_name}/{remote_path}")
            except Exception as e:
                logger.error(f"Upload failed ({local_path}): {e}")



        logger.info(f"Upload complete: {uploaded}/{len(to_upload)} file(s) mirrored to GCS.")
        
        # ---------- CLEANUP ----------
        try:
            for root, dirs, files in os.walk(self.base_path, topdown=False):
                for f in files:
                    os.remove(os.path.join(root, f))
                for d in dirs:
                    os.rmdir(os.path.join(root, d))
            logger.info(f"Cache cleared: {self.base_path}")
        except Exception as e:
            logger.error(f"Cache cleanup failed: {e}")

