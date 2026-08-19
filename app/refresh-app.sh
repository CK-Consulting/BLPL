#!/bin/bash

docker compose -f /opt/blpl/app/docker-compose.yml down
docker compose -f /opt/blpl/app/docker-compose.yml build --no-cache
#docker compose -f /opt/blpl/app/docker-compose.yml up -d
docker compose -f /opt/blpl/app/docker-compose.yml --profile scanning up -d
