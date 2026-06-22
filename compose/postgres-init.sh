#!/bin/bash
# Runs once when the postgres container is first initialized
# (docker-entrypoint-initdb.d/*.sh). Creates the additional databases that
# jarvis-auth and jarvis-config-service need.
#
# pgvector extension lives in jarvis_command_center only (CC's memory
# embeddings). The other DBs don't need it.
#
# Note: the T9 from-source whisper/tts lanes deliberately do NOT get a Postgres
# DB here. jarvis_settings_client.SettingsService.get() swallows DB errors and
# falls through to env fallbacks (service.py: try/except around the query), so
# those services point DATABASE_URL at a throwaway sqlite file and resolve
# whisper.model_path / tts.provider from the WHISPER_MODEL / TTS_PROVIDER env
# the image bakes. No settings table, no migrations needed.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE DATABASE jarvis_auth;
    CREATE DATABASE jarvis_config;
EOSQL
