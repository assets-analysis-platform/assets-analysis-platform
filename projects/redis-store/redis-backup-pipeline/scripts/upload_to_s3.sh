#!/bin/bash

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <bucket_name> <rdb_file>"
    exit 1
fi

bucket_name=$1    # ex. "aap-dev-bucket"
rdb_file=$2       # ex. /data/redis_backups/date=2024-03-13_14-38

aws s3 cp $rdb_file s3://bucket_name/redis_backups/ && echo "Redis Backup copied to S3"

exit 0