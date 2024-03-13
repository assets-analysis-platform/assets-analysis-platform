#!/bin/bash

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <bucket_name> <rdb_file>"
    exit 1
fi

bucket_name=$1    # ex. "aap-dev-bucket"
rdb_file=$2       # ex. /data/redis_backups/date=2024-03-13_15-51/dump.rdb

date_dir=$(echo "$rdb_file" | grep -o 'date=[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}_[0-9]\{2\}-[0-9]\{2\}') # extract 'YYYY-MM-DD_HH-MM' from path
s3_dest=s3://$bucket_name/redis_backups/$date_dir/dump.rdb

aws s3 cp $rdb_file $s3_dest && echo "Redis Backup successfully copied to S3"
exit 0