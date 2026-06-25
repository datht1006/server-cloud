from typing import List, Dict

class SearchService:

    def __init__(self, r2_manager):
        self.r2 = r2_manager

    def search(self, keyword: str) -> List[Dict]:

        keyword = keyword.strip().lower()

        if not keyword:
            return []

        result = self.r2.client.list_objects_v2(
            Bucket=self.r2.bucket
        )

        files = []

        for obj in result.get("Contents", []):

            key = obj["Key"]

            if key.endswith("/"):
                continue

            if keyword in key.lower():

                files.append({
                    "name": key.split("/")[-1],
                    "key": key,
                    "path": "/".join(key.split("/")[:-1]),
                    "size": obj["Size"],
                    "url": f"/api/file/{key}"
                })

        files.sort(
            key=lambda x: x["name"].lower()
        )

        return files

    def search_by_extension(
        self,
        extension: str
    ) -> List[Dict]:

        extension = extension.lower()

        result = self.r2.client.list_objects_v2(
            Bucket=self.r2.bucket
        )

        files = []

        for obj in result.get("Contents", []):

            key = obj["Key"]

            if key.endswith("/"):
                continue

            if key.lower().endswith(extension):

                files.append({
                    "name": key.split("/")[-1],
                    "key": key,
                    "size": obj["Size"],
                    "url": f"/api/file/{key}"
                })

        return files

    def search_in_folder(
        self,
        folder_path: str,
        keyword: str
    ):

        result = self.r2.client.list_objects_v2(
            Bucket=self.r2.bucket,
            Prefix=folder_path
        )

        files = []

        keyword = keyword.lower()

        for obj in result.get("Contents", []):

            key = obj["Key"]

            if key.endswith("/"):
                continue

            if keyword in key.lower():

                files.append({
                    "name": key.split("/")[-1],
                    "key": key,
                    "size": obj["Size"],
                    "url": f"/api/file/{key}"
                })

        return files

