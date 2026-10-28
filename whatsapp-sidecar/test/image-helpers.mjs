// Tiny well-formed image headers (enough for the magic-byte check) padded to a size. Not real pictures.
const HEADS = {
  'image/jpeg': Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0, 0, 0, 0, 0, 0, 0]),
  'image/png': Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]),
  'image/gif': Buffer.from('GIF89a000000', 'latin1'),
  'image/webp': Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WEBP')]),
}

export function ImgBuf(mime = 'image/png', size = 64, fill = 7) {
  const head = HEADS[mime]
  return Buffer.concat([head, Buffer.alloc(Math.max(0, size - head.length), fill)])
}
