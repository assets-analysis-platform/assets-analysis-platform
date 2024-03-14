#!/bin/bash

REDIS_CLI="/usr/local/bin/redis-cli"
RDB_DUMP_DEFAULT_PATH="/data/dump.rdb"
S3_BUCKET_NAME="aap-dev-bucket"

DIR=`date +%Y-%m-%d_%H-%M`
DATE_DIR="date=$DIR"
DEST=/data/redis_backups/$DATE_DIR
mkdir --parents $DEST

# create backup
echo save| $REDIS_CLI
mv $RDB_DUMP_DEFAULT_PATH $DEST

# upload snapshot to AWS S3
RDB_FILE=$DEST/dump.rdb                                           # dump.rdb to be uploaded
S3_DEST=s3://$S3_BUCKET_NAME/redis_backups/$DATE_DIR/dump.rdb     # S3 destination

aws s3 cp $RDB_FILE $S3_DEST && echo "Redis Backup successfully copied to S3"
exit 0