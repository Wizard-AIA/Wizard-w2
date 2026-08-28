const fs = require('fs');
const path = require('path');

const frontendDir = path.resolve(__dirname, '..');
const staticSrc = path.join(frontendDir, '.next', 'static');
const staticDest = path.join(frontendDir, '.next', 'standalone', '.next', 'static');

const publicSrc = path.join(frontendDir, 'public');
const publicDest = path.join(frontendDir, '.next', 'standalone', 'public');

try {
  if (fs.existsSync(staticSrc)) {
    fs.mkdirSync(path.dirname(staticDest), { recursive: true });
    fs.cpSync(staticSrc, staticDest, { recursive: true, force: true });
    console.log('[standalone] Copied .next/static -> .next/standalone/.next/static');
  }
  if (fs.existsSync(publicSrc)) {
    fs.mkdirSync(publicDest, { recursive: true });
    fs.cpSync(publicSrc, publicDest, { recursive: true, force: true });
    console.log('[standalone] Copied public -> .next/standalone/public');
  }
} catch (err) {
  console.warn('[standalone] Asset copy warning:', err.message);
}
