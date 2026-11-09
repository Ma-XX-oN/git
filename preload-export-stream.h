/* Restricted Bash export -p decoder. Included in preload-context.c after
 * clear_worker_environment(); never interpret commands or expansions. */
/* This is a private implementation fragment, not a standalone API header. */
#ifdef PRELOAD_EXPORT_STREAM_IMPLEMENTATION
struct export_pair { char *name, *value; };
static int export_hex(unsigned char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
static int export_cp(char *out, size_t *n, unsigned long cp) {
  if (!cp || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) return -1;
  if (cp < 128) out[(*n)++] = cp;
  else if (cp < 2048) {
    out[(*n)++] = 0xc0 | (cp >> 6); out[(*n)++] = 0x80 | (cp & 63);
  } else if (cp < 65536) {
    out[(*n)++] = 0xe0 | (cp >> 12);
    out[(*n)++] = 0x80 | ((cp >> 6) & 63);
    out[(*n)++] = 0x80 | (cp & 63);
  } else {
    out[(*n)++] = 0xf0 | (cp >> 18);
    out[(*n)++] = 0x80 | ((cp >> 12) & 63);
    out[(*n)++] = 0x80 | ((cp >> 6) & 63);
    out[(*n)++] = 0x80 | (cp & 63);
  }
  return 0;
}
static int decode_export_value(const char *p, size_t len, char **value) {
  int ansi = len >= 3 && p[0] == '$' && p[1] == '\'';
  char quote = ansi ? '\'' : '"', *out;
  size_t i, n = 0;
  if (ansi) { p += 2; len -= 2; }
  else { if (len < 2 || p[0] != '"') return -1; p++; len--; }
  if (!len || p[len - 1] != quote) return -1;
  len--;
  out = malloc(len * 4 + 1);
  if (!out) return -1;
  for (i = 0; i < len; i++) {
    unsigned char ch = p[i];
    unsigned long cp;
    int d, count, limit;
    if (!ch || ch == quote) goto fail;
    if (ch != '\\') { out[n++] = ch; continue; }
    if (++i == len) goto fail;
    ch = p[i];
    if (!ansi) {
      if (ch == '$' || ch == 0x60 || ch == '"' || ch == '\\')
        out[n++] = ch;
      else if (ch != '\n') {
        out[n++] = '\\'; out[n++] = ch;
      }
      continue;
    }
    switch (ch) {
    case 'a': ch = '\a'; break;
    case 'b': ch = '\b'; break;
    case 'e': case 'E': ch = 27; break;
    case 'f': ch = '\f'; break;
    case 'n': ch = '\n'; break;
    case 'r': ch = '\r'; break;
    case 't': ch = '\t'; break;
    case 'v': ch = '\v'; break;
    case '\\': case '\'': case '"': case '?': break;
    case 'c': if (++i == len) goto fail; ch = p[i] & 31; break;
    case 'x': case 'u': case 'U':
      limit = ch == 'x' ? 2 : ch == 'u' ? 4 : 8;
      cp = 0; count = 0;
      while (count < limit && i + 1 < len &&
             (d = export_hex(p[i + 1])) >= 0) {
        cp = cp * 16 + d; i++; count++;
      }
      if (!count || (limit > 2 && count != limit)) goto fail;
      if (limit == 2) {
        /* Bash hex escapes describe raw bytes, not Unicode code points. */
        if (!cp) goto fail;
        out[n++] = (char)cp;
      } else if (export_cp(out, &n, cp))
        goto fail;
      continue;
    default:
      if (ch >= '0' && ch <= '7') {
        cp = ch - '0';
        for (count = 1; count < 3 && i + 1 < len &&
             p[i + 1] >= '0' && p[i + 1] <= '7'; count++)
          cp = cp * 8 + (unsigned char)p[++i] - '0';
        ch = cp;
      } else out[n++] = '\\';
      break;
    }
    if (!ch) goto fail;
    out[n++] = ch;
  }
  out[n] = '\0';
  if (!is_utf8(out)) goto fail;
  *value = out;
  return 0;
fail:
  free(out);
  return -1;
}
static int export_name_ok(const char *p, size_t len) {
  size_t i;
  if (!len || !((p[0] >= 'a' && p[0] <= 'z') ||
                (p[0] >= 'A' && p[0] <= 'Z') || p[0] == '_')) return 0;
  for (i = 1; i < len; i++)
    if (!((p[i] >= 'a' && p[i] <= 'z') ||
          (p[i] >= 'A' && p[i] <= 'Z') ||
          (p[i] >= '0' && p[i] <= '9') || p[i] == '_')) return 0;
  return 1;
}
static int apply_export_stream(const char *buf, size_t len) {
  struct export_pair *pairs = NULL, *grown;
  size_t off = 0, used = 0, cap = 0, i;
  int rc = -1;
  if (!len || buf[len - 1] != '\n' || memchr(buf, '\0', len)) return -1;
  while (off < len) {
    const char *line = buf + off;
    const char *end = memchr(line, '\n', len - off);
    const char *name, *eq;
    size_t line_len, name_len;
    if (!end) goto cleanup;
    line_len = end - line;
    if (line_len < 11 || memcmp(line, "declare -x ", 11)) goto cleanup;
    name = line + 11;
    eq = memchr(name, '=', line_len - 11);
    name_len = eq ? (size_t)(eq - name) : line_len - 11;
    if (!export_name_ok(name, name_len)) goto cleanup;
    off = (size_t)(end - buf) + 1;
    if (!eq) continue;
    if (used == cap) {
      size_t next = cap ? cap * 2 : 32;
      if (next > len) goto cleanup;
      grown = realloc(pairs, next * sizeof(*pairs));
      if (!grown) goto cleanup;
      pairs = grown; cap = next;
    }
    pairs[used].name = malloc(name_len + 1);
    if (!pairs[used].name) goto cleanup;
    memcpy(pairs[used].name, name, name_len);
    pairs[used].name[name_len] = '\0';
    pairs[used].value = NULL;
    used++;
    if (decode_export_value(eq + 1, end - eq - 1,
                            &pairs[used - 1].value)) goto cleanup;
  }
  if (clear_worker_environment()) goto cleanup;
  for (i = 0; i < used; i++)
    if (setenv(pairs[i].name, pairs[i].value, 1)) goto cleanup;
  rc = 0;
cleanup:
  for (i = 0; i < used; i++) {
    free(pairs[i].name); free(pairs[i].value);
  }
  free(pairs);
  return rc;
}

#endif /* PRELOAD_EXPORT_STREAM_IMPLEMENTATION */
