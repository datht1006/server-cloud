import os
import boto3

# ===== Cloudflare R2 =====
ACCOUNT_ID = "b2d37347d30fcf240921fd65db9f5155"
ACCESS_KEY = "7c247d7125df074698e64081e6a4eee7"
SECRET_KEY = "822001efe086931d5dd2527fae7438ca463e3e230ada45d7ba48e3ef2b4af95e"
BUCKET = "hpdq2"

# ===== Folder local =====
LOCAL_PATH = r"D:\New folder (3)\HPDQ1.NM.CD5 (FILESRV01)"

s3 = boto3.client(
    service_name='s3',
    endpoint_url=f'https://{ACCOUNT_ID}.r2.cloudflarestorage.com',
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
)

for root, dirs, files in os.walk(LOCAL_PATH):

    rel_path = os.path.relpath(root, LOCAL_PATH)

    if rel_path == ".":
        continue

    folder_key = rel_path.replace("\\", "/") + "/"

    print("Creating:", folder_key)

    s3.put_object(
        Bucket=BUCKET,
        Key=folder_key
    )

print("Done!")