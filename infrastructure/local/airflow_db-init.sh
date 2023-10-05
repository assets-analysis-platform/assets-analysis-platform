#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE ROLE airflow WITH LOGIN PASSWORD 'changeme';
  CREATE DATABASE airflow OWNER airflow;
EOSQL