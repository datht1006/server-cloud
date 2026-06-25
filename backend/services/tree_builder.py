from collections import defaultdict

class TreeBuilder:

    def __init__(self, r2_manager):
        self.r2 = r2_manager

    def build_tree(self):

        result = self.r2.client.list_objects_v2(
            Bucket=self.r2.bucket
        )

        root = {}

        for obj in result.get("Contents", []):

            key = obj["Key"]

            if not key.endswith("/"):
                continue

            parts = key.strip("/").split("/")

            current = root

            for part in parts:

                if part not in current:
                    current[part] = {}

                current = current[part]

        return self._convert(root)

    def _convert(self, node):

        items = []

        for name, children in sorted(node.items()):

            items.append({
                "name": name,
                "children": self._convert(children)
            })

        return items

    def build_flat_tree(self):

        result = self.r2.client.list_objects_v2(
            Bucket=self.r2.bucket
        )

        folders = []

        for obj in result.get("Contents", []):

            key = obj["Key"]

            if key.endswith("/"):

                folders.append({
                    "name": key.rstrip("/").split("/")[-1],
                    "path": key
                })

        return folders

    def get_children(self, path=""):

        result = self.r2.client.list_objects_v2(
            Bucket=self.r2.bucket,
            Prefix=path,
            Delimiter="/"
        )

        children = []

        for item in result.get("CommonPrefixes", []):

            prefix = item["Prefix"]

            children.append({
                "name": prefix.rstrip("/").split("/")[-1],
                "path": prefix
            })

        return children

