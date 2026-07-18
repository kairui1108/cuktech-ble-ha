#pragma once

#include <stdbool.h>

void ota_update_init(void);
bool ota_update_start(const char *url);
