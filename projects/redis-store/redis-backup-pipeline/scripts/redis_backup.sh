#!/bin/bash

rdb_default_path="/data/dump.rdb"
redis_cli_cmd="redis-cli"

DIR=`date +%Y-%m-%d_%H-%M`
DEST=/data/redis_backups/date=$DIR
mkdir --parents $DEST

echo save| $redis_cli_cmd
mv $rdb_default_path $DEST

echo $DEST/dump.rdb  # return the path to the created backup