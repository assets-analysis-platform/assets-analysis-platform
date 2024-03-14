#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE ROLE lenses WITH LOGIN PASSWORD 'changeme';
  CREATE DATABASE lenses OWNER lenses;
EOSQL