/* Minimal ZIP writer for the batch download.
 *
 * Builds a "stored" (uncompressed) ZIP from Blobs entirely in the browser, so the batch
 * results can be bundled without sending them back to the server. Converted files are
 * mostly compressed already (JPG, MP4, DOCX...), so storing them costs little.
 * Limits: no ZIP64, so the archive must stay under 4 GB and 65535 entries.
 * Works in browsers and in Node 18+ (used by tests/test_batch.py).
 */
(function (root) {
  'use strict';

  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes, crc = 0) {
    let c = (crc ^ 0xffffffff) >>> 0;
    for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  // ZIP stores local time in MS-DOS format (2-second resolution, years 1980-2107).
  function dosDateTime(d) {
    const year = Math.min(Math.max(d.getFullYear(), 1980), 2107);
    return {
      time: (d.getHours() << 11) | (d.getMinutes() << 5) | Math.floor(d.getSeconds() / 2),
      date: ((year - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate(),
    };
  }

  // "a.pdf", "a.pdf" -> "a.pdf", "a (2).pdf". Keeps compound extensions like .tar.gz together.
  function uniqueNames(names) {
    const seen = new Set();
    return names.map(raw => {
      const name = (raw || 'file').replace(/[\\/]+/g, '_').replace(/^\.+/, '') || 'file';
      if (!seen.has(name.toLowerCase())) { seen.add(name.toLowerCase()); return name; }
      const m = name.match(/^(.*?)((?:\.tar)?\.[^.]+)?$/);
      const stem = m[1] || 'file', ext = m[2] || '';
      for (let n = 2; ; n++) {
        const candidate = `${stem} (${n})${ext}`;
        if (!seen.has(candidate.toLowerCase())) { seen.add(candidate.toLowerCase()); return candidate; }
      }
    });
  }

  const MAX_ZIP_BYTES = 0xffffffff;

  /**
   * entries: [{ name: string, blob: Blob, date?: Date }]
   * Returns a Promise<Blob> with type application/zip. Throws if the result would need ZIP64.
   */
  async function makeZip(entries) {
    if (entries.length > 0xffff) throw new Error('Too many files for one zip.');
    const enc = new TextEncoder();
    const names = uniqueNames(entries.map(e => e.name));
    const parts = [], central = [];
    let offset = 0;

    for (let i = 0; i < entries.length; i++) {
      const { blob, date } = entries[i];
      const nameBytes = enc.encode(names[i]);
      const data = new Uint8Array(await blob.arrayBuffer());
      const crc = crc32(data);
      const { time, date: dosDate } = dosDateTime(date || new Date());
      if (offset + 30 + nameBytes.length + data.length > MAX_ZIP_BYTES) {
        throw new Error('The results are too large for one zip (over 4 GB). Download the files one by one.');
      }

      const local = new DataView(new ArrayBuffer(30));
      local.setUint32(0, 0x04034b50, true);
      local.setUint16(4, 20, true);         // version needed
      local.setUint16(6, 0x0800, true);     // UTF-8 names
      local.setUint16(8, 0, true);          // stored
      local.setUint16(10, time, true);
      local.setUint16(12, dosDate, true);
      local.setUint32(14, crc, true);
      local.setUint32(18, data.length, true);
      local.setUint32(22, data.length, true);
      local.setUint16(26, nameBytes.length, true);
      local.setUint16(28, 0, true);
      parts.push(local.buffer, nameBytes, blob);

      const cd = new DataView(new ArrayBuffer(46));
      cd.setUint32(0, 0x02014b50, true);
      cd.setUint16(4, 20, true);            // version made by
      cd.setUint16(6, 20, true);            // version needed
      cd.setUint16(8, 0x0800, true);
      cd.setUint16(10, 0, true);
      cd.setUint16(12, time, true);
      cd.setUint16(14, dosDate, true);
      cd.setUint32(16, crc, true);
      cd.setUint32(20, data.length, true);
      cd.setUint32(24, data.length, true);
      cd.setUint16(28, nameBytes.length, true);
      // extra, comment, disk, internal attrs stay 0
      cd.setUint32(38, 0, true);            // external attrs
      cd.setUint32(42, offset, true);
      central.push(cd.buffer, nameBytes);

      offset += 30 + nameBytes.length + data.length;
    }

    const cdSize = central.reduce((n, p) => n + p.byteLength, 0);
    if (offset + cdSize + 22 > MAX_ZIP_BYTES) {
      throw new Error('The results are too large for one zip (over 4 GB). Download the files one by one.');
    }
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, entries.length, true);
    end.setUint16(10, entries.length, true);
    end.setUint32(12, cdSize, true);
    end.setUint32(16, offset, true);
    return new Blob([...parts, ...central, end.buffer], { type: 'application/zip' });
  }

  const api = { crc32, makeZip, uniqueNames };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ZipWriter = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
