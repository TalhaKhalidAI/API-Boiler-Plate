# App/core/bucket_policy.py
"""MinIO bucket policy: public read for assets, private for originals."""


def build_default_bucket_policy(bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadForPublicAssets",
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},           # ← anyone, no auth
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/channels/*/banner.jpg",
                    f"arn:aws:s3:::{bucket}/channels/*/avatar.jpg",
                    f"arn:aws:s3:::{bucket}/channels/*/videos/*/final/*",
                    f"arn:aws:s3:::{bucket}/channels/*/videos/*/thumbnail.jpg",
                    f"arn:aws:s3:::{bucket}/users/*/profile_pic.jpg",
                    f"arn:aws:s3:::{bucket}/playlists/*/thumbnail.jpg",
                ],
            }
        ],
    }