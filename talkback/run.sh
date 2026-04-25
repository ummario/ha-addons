#!/usr/bin/with-contenv bashio

# Ler opções do add-on
export UFP_ADDRESS=$(bashio::config 'ufp_address')
export UFP_PORT=$(bashio::config 'ufp_port')
export UFP_USERNAME=$(bashio::config 'ufp_username')
export UFP_PASSWORD=$(bashio::config 'ufp_password')
export UFP_API_KEY=$(bashio::config 'ufp_api_key')
export UFP_SSL_VERIFY=$(bashio::config 'ufp_ssl_verify')
export UFP_CAMERA_ID=$(bashio::config 'ufp_camera_id')
export LOG_LEVEL=$(bashio::config 'log_level')
export TALKBACK_PORT=3006
export TALKBACK_HOST=0.0.0.0

# Ingress path do HA (ex: /api/hassio_ingress/xxxxx)
export INGRESS_PATH=$(bashio::addon.ingress_url)

bashio::log.info "A arrancar Talkback Campainha..."
bashio::log.info "NVR: ${UFP_ADDRESS}:${UFP_PORT}"
bashio::log.info "Camera: ${UFP_CAMERA_ID}"
bashio::log.info "Ingress: ${INGRESS_PATH}"

cd /app
exec python3 talkback_server.py
