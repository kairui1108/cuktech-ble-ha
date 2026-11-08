cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# Firmware version of this ESP32 image.  Keep in sync with FW_VERSION in
# main/config.h (the /api and MQTT payloads report that macro).
set(PROJECT_VER "1.1.5")
project(cuktech_ble)

# Merged images: use IDF's built-in `idf.py merge-bin`.  It merges exactly the
# segments and offsets in build/flash_args (bootloader at 0x1000 on ESP32,
# 0x0 on C3/S3; partition table at 0x8000; otadata at 0xf000; ota_0 at
# 0x20000), so nothing here has to duplicate partitions.csv.  A hand-written
# merge target with hard-coded offsets silently drifted from the table once
# already (it still wrote the app at 0x10000 after the layout moved to
# 0x20000).
#
#   idf.py build && idf.py merge-bin   ->   build/merged-binary.bin
