#!/bin/bash

docker compose down
docker compose build --no-cache
#docker compose /opt/blpl/app/docker-compose.yml up -d
docker compose --profile scanning up -d
