idf_component_register(
    SRCS "main.c" "ble_manager.c" "miot_auth.c" "miot_protocol.c" "config_store.c"
         "http_server.c" "bemfa.c" "device_state.c" "uplink.c" "ota.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_event nvs_flash esp_netif mqtt esp_timer bt esp_http_server esp_coex app_update
    PRIV_REQUIRES json esp_http_client
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-error)
