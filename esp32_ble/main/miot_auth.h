#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <mbedtls/md.h>
#include <mbedtls/ccm.h>

// ============================================================
// MiOT BLE Authentication & Crypto
// ============================================================

typedef struct {
    uint8_t dev_key[16];
    uint8_t app_key[16];
    uint8_t dev_iv[4];
    uint8_t app_iv[4];
} SessionKeys;

static inline bool hmac_sha256(const uint8_t* key, size_t key_len,
                                const uint8_t* data, size_t data_len,
                                uint8_t out_32bytes[32]) {
    mbedtls_md_context_t ctx;
    mbedtls_md_init(&ctx);
    if (mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1) != 0) {
        mbedtls_md_free(&ctx); return false;
    }
    int ret = mbedtls_md_hmac_starts(&ctx, key, key_len);
    if (ret == 0) ret = mbedtls_md_hmac_update(&ctx, data, data_len);
    if (ret == 0) ret = mbedtls_md_hmac_finish(&ctx, out_32bytes);
    mbedtls_md_free(&ctx);
    return ret == 0;
}

static inline bool hkdf_derive(const uint8_t* salt, size_t salt_len,
                                const uint8_t* ikm, size_t ikm_len,
                                const uint8_t* info, size_t info_len,
                                uint8_t* output, size_t output_len) {
    uint8_t prk[32];
    if (!hmac_sha256(salt, salt_len, ikm, ikm_len, prk)) return false;
    uint8_t T[32];
    size_t t_len = 0;
    uint8_t counter = 1;
    size_t pos = 0;
    while (pos < output_len) {
        uint8_t input[288];
        size_t input_len = 0;
        if (t_len > 0) { memcpy(input, T, t_len); input_len += t_len; }
        if (info_len > 0) { memcpy(input + input_len, info, info_len); input_len += info_len; }
        input[input_len++] = counter;
        if (!hmac_sha256(prk, 32, input, input_len, T)) return false;
        t_len = 32;
        size_t copy = (output_len - pos < 32) ? (output_len - pos) : 32;
        memcpy(output + pos, T, copy);
        pos += copy;
        counter++;
    }
    return true;
}

static inline bool aes_ccm_encrypt(const uint8_t* key, size_t key_len,
                                    const uint8_t* nonce, size_t nonce_len,
                                    const uint8_t* plaintext, size_t pt_len,
                                    uint8_t* ciphertext_tag, size_t* out_len,
                                    size_t tag_len) {
    mbedtls_ccm_context ctx;
    mbedtls_ccm_init(&ctx);
    if (mbedtls_ccm_setkey(&ctx, MBEDTLS_CIPHER_ID_AES, key, key_len * 8) != 0) {
        mbedtls_ccm_free(&ctx); return false;
    }
    *out_len = pt_len + tag_len;
    int ret = mbedtls_ccm_encrypt_and_tag(&ctx, pt_len, nonce, nonce_len,
                                           NULL, 0, plaintext, ciphertext_tag,
                                           ciphertext_tag + pt_len, tag_len);
    mbedtls_ccm_free(&ctx);
    return ret == 0;
}

static inline bool aes_ccm_decrypt(const uint8_t* key, size_t key_len,
                                    const uint8_t* nonce, size_t nonce_len,
                                    const uint8_t* data, size_t data_len,
                                    uint8_t* plaintext, size_t* pt_len,
                                    size_t tag_len) {
    if (data_len < tag_len) return false;
    mbedtls_ccm_context ctx;
    mbedtls_ccm_init(&ctx);
    if (mbedtls_ccm_setkey(&ctx, MBEDTLS_CIPHER_ID_AES, key, key_len * 8) != 0) {
        mbedtls_ccm_free(&ctx); return false;
    }
    *pt_len = data_len - tag_len;
    int ret = mbedtls_ccm_auth_decrypt(&ctx, *pt_len, nonce, nonce_len,
                                        NULL, 0, data, plaintext,
                                        data + *pt_len, tag_len);
    mbedtls_ccm_free(&ctx);
    return ret == 0;
}

static inline bool secure_memcmp(const uint8_t* a, const uint8_t* b, size_t len) {
    uint8_t diff = 0;
    for (size_t i = 0; i < len; i++) diff |= a[i] ^ b[i];
    return diff == 0;
}

static inline bool derive_session_keys(const uint8_t* token, size_t token_len,
                                        const uint8_t* rand_key, const uint8_t* dev_random,
                                        SessionKeys* keys) {
    uint8_t salt[32];
    memcpy(salt, rand_key, 16);
    memcpy(salt + 16, dev_random, 16);
    const char info[] = "mible-login-info";
    uint8_t derived[64];
    if (!hkdf_derive(salt, 32, token, token_len,
                     (const uint8_t*)info, strlen(info), derived, 64)) return false;
    memcpy(keys->dev_key, derived, 16);
    memcpy(keys->app_key, derived + 16, 16);
    memcpy(keys->dev_iv, derived + 32, 4);
    memcpy(keys->app_iv, derived + 36, 4);
    return true;
}

static inline bool encrypt_command(const SessionKeys* keys, uint32_t* send_it,
                                    const uint8_t* plaintext, size_t pt_len,
                                    uint8_t* out, size_t* out_len) {
    uint8_t nonce[12];
    memcpy(nonce, keys->app_iv, 4);
    memset(nonce + 4, 0, 4);
    nonce[8]  = *send_it & 0xFF;
    nonce[9]  = (*send_it >> 8) & 0xFF;
    nonce[10] = (*send_it >> 16) & 0xFF;
    nonce[11] = (*send_it >> 24) & 0xFF;
    uint8_t ct[512];
    size_t ct_len;
    if (!aes_ccm_encrypt(keys->app_key, 16, nonce, 12, plaintext, pt_len, ct, &ct_len, 4)) return false;
    uint32_t this_it = (*send_it)++;
    out[0] = this_it & 0xFF;
    out[1] = (this_it >> 8) & 0xFF;
    *out_len = 2 + ct_len;
    memcpy(out + 2, ct, ct_len);
    return true;
}

static inline bool decrypt_response(const SessionKeys* keys,
                                     const uint8_t* data, size_t data_len,
                                     uint8_t* plaintext, size_t* pt_len) {
    if (data_len < 6) return false;
    uint8_t nonce[12];
    memcpy(nonce, keys->dev_iv, 4);
    memset(nonce + 4, 0, 4);
    nonce[8]  = data[0];
    nonce[9]  = data[1];
    nonce[10] = 0;
    nonce[11] = 0;
    return aes_ccm_decrypt(keys->dev_key, 16, nonce, 12,
                           data + 2, data_len - 2, plaintext, pt_len, 4);
}

static inline bool verify_device_hmac(const uint8_t* dev_key,
                                       const uint8_t* salt_inv, size_t salt_inv_len,
                                       const uint8_t* expected_hmac) {
    uint8_t computed[32];
    if (!hmac_sha256(dev_key, 16, salt_inv, salt_inv_len, computed)) return false;
    return secure_memcmp(computed, expected_hmac, 32);
}

static inline bool compute_app_hmac(const SessionKeys* keys,
                                     const uint8_t* salt, size_t salt_len,
                                     uint8_t out_hmac[32]) {
    return hmac_sha256(keys->app_key, 16, salt, salt_len, out_hmac);
}
