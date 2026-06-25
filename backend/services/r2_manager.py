
import os
import boto3
from dotenv import load_dotenv


class R2Manager:

    def __init__(self):

        load_dotenv()

        self.bucket = os.getenv("R2_BUCKET")

        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
            aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
            aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
            region_name="auto"
        )

    # =========================
    # Folder
    # =========================

    def get_folders(self, path=""):

        result = self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=path,
            Delimiter="/"
        )

        folders = []

        for item in result.get("CommonPrefixes", []):

            prefix = item["Prefix"]

            folders.append({
                "name": prefix.rstrip("/").split("/")[-1],
                "path": prefix
            })

        return folders

    def create_folder(self, name, path=""):

        folder_key = path + name + "/"

        self.client.put_object(
            Bucket=self.bucket,
            Key=folder_key,
            Body=b""
        )

        return {
            "success": True,
            "path": folder_key
        }

    def delete_folder(self, path):

        result = self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=path
        )

        for obj in result.get("Contents", []):

            self.client.delete_object(
                Bucket=self.bucket,
                Key=obj["Key"]
            )

        return {
            "success": True
        }

    # =========================
    # Files
    # =========================

    def get_files(self, path=""):

        result = self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=path,
            Delimiter="/"
        )

        folders = []

        for item in result.get("CommonPrefixes", []):

            prefix = item["Prefix"]

            folders.append({
                "name": prefix.rstrip("/").split("/")[-1],
                "path": prefix
            })

        files = []

        for obj in result.get("Contents", []):

            if obj["Key"] == path:
                continue

            if obj["Key"].endswith("/"):
                continue

            files.append({
                "name": obj["Key"].split("/")[-1],
                "key": obj["Key"],
                "size": obj["Size"],
                "url": f"/api/file/{obj['Key']}"
            })

        return {
            "folders": folders,
            "files": files
        }

    def upload_file(self, file_bytes, filename, path=""):

        key = path + filename

        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=file_bytes
        )

        return {
            "success": True,
            "key": key
        }

    def delete_file(self, key):

        self.client.delete_object(
            Bucket=self.bucket,
            Key=key
        )

        return {
            "success": True
        }

    # =========================
    # Rename
    # =========================

    def rename_file(self, old_key, new_name):

        folder = ""

        if "/" in old_key:
            folder = old_key.rsplit("/", 1)[0] + "/"

        new_key = folder + new_name

        self.client.copy_object(
            Bucket=self.bucket,
            CopySource={
                "Bucket": self.bucket,
                "Key": old_key
            },
            Key=new_key
        )

        self.client.delete_object(
            Bucket=self.bucket,
            Key=old_key
        )

        return {
            "success": True,
            "key": new_key
        }

    def move_file(self, old_key, new_folder):

        filename = old_key.split("/")[-1]

        new_key = (
            new_folder.rstrip("/")
            + "/"
            + filename
        )

        self.client.copy_object(
            Bucket=self.bucket,
            CopySource={
                "Bucket": self.bucket,
                "Key": old_key
            },
            Key=new_key
        )

        self.client.delete_object(
            Bucket=self.bucket,
            Key=old_key
        )

        return {
            "success": True,
            "key": new_key
        }

    # =========================
    # Search
    # =========================

    def search(self, keyword):

        result = self.client.list_objects_v2(
            Bucket=self.bucket
        )

        files = []

        for obj in result.get("Contents", []):

            if obj["Key"].endswith("/"):
                continue

            if keyword.lower() in obj["Key"].lower():

                files.append({
                    "name": obj["Key"].split("/")[-1],
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "url": f"/api/file/{obj['Key']}"
                })

        return files

    # =========================
    # Preview
    # =========================

    def get_presigned_url(self, key):

        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key
            },
            ExpiresIn=3600
        )

