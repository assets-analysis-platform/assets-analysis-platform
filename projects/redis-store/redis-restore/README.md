# Redis restore

### Restore process guide (to be done by CI/CD or manually)

1. Make sure that 'appendonly' in redis.config is set to no.
```shell
appendonly no
```
AOF stands for Append-Only File, which will instruct Redis to log all operations in a .aof file.
Since we're restoring a backup, we need to disable AOF before restoring the data as we don't want 
Redis to log all these operations.

2. Stop Redis server
```shell
docker-compose stop redis
```

3. Restore Redis database
```shell
cp <BACKUP_FILE> /data/dump.rdb
```

4. Start Redis server
```shell
docker-compose start redis
```

Additional Resources:
- https://simplebackups.com/blog/the-complete-redis-backup-guide-with-examples/
- https://redis.io/docs/manual/persistence/