#!/usr/bin/env bash
set -Eeuo pipefail

: "${MONGO_HOST:?MONGO_HOST is required}"
: "${MONGO_ROOT_USERNAME:?MONGO_ROOT_USERNAME is required}"
: "${MONGO_ROOT_PASSWORD:?MONGO_ROOT_PASSWORD is required}"
: "${MONGO_APP_USERNAME:?MONGO_APP_USERNAME is required}"
: "${MONGO_APP_PASSWORD:?MONGO_APP_PASSWORD is required}"
: "${MONGO_APP_DB:?MONGO_APP_DB is required}"

for attempt in $(seq 1 60); do
  if mongosh --quiet \
    --host "$MONGO_HOST" \
    --username "$MONGO_ROOT_USERNAME" \
    --password "$MONGO_ROOT_PASSWORD" \
    --authenticationDatabase admin \
    --eval '
const databaseName = process.env.MONGO_APP_DB;
const username = process.env.MONGO_APP_USERNAME;
const password = process.env.MONGO_APP_PASSWORD;
const adminDb = db.getSiblingDB("admin");
const existing = adminDb.getUser(username);
const roles = [{ role: "readWrite", db: databaseName }];

if (existing) {
  adminDb.updateUser(username, { pwd: password, roles });
  print("MongoDB application user updated");
} else {
  adminDb.createUser({ user: username, pwd: password, roles });
  print("MongoDB application user created");
}
'; then
    exit 0
  fi
  if [ "$attempt" -lt 60 ]; then
    sleep 2
  fi
done

printf 'MongoDB application user initialization timed out\\n' >&2
exit 1
