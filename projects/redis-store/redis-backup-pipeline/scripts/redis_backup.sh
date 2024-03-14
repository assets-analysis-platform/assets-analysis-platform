#!/bin/bash
###################################################################################################
# Script Name: redis_backup.sh
# Description: This script creates a backup of Redis data and uploads it to AWS S3.
# Version: 1.0
# Author: Marcin Krolczyk
# Date: 14.03.2024
###################################################################################################

# Example usage: ./redis_backup.sh aap-dev-bucket

if [ "$#" -ne 1 ]; then
    echo "Usage: $(basename -- "$0") <bucket_name>"
    exit 1
fi

REDIS_CLI="/usr/local/bin/redis-cli"
RDB_BACKUP_DUMP_PATH="/data/dump.rdb"                             # the default dump path is the directory from which the Redis server got started from
S3_BUCKET_NAME=$1

DIR=`date +%Y-%m-%d_%H-%M`
DATE_DIR="date=$DIR"
DEST=/data/redis_backups/$DATE_DIR
mkdir --parents $DEST

# create backup
echo save| $REDIS_CLI
mv $RDB_BACKUP_DUMP_PATH $DEST

# upload snapshot to AWS S3
RDB_FILE=$DEST/dump.rdb                                           # dump.rdb to be uploaded
S3_DEST=s3://$S3_BUCKET_NAME/redis_backups/$DATE_DIR/dump.rdb     # S3 destination

aws s3 cp $RDB_FILE $S3_DEST && echo "Redis Backup successfully copied to S3"
exit 0