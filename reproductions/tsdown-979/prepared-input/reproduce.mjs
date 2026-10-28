import { readFileSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

rmSync('dist', { recursive: true, force: true })

const build = spawnSync('pnpm', ['build'], {
  shell: process.platform === 'win32',
  stdio: 'inherit',
})

if (build.error) {
  console.error(build.error.message)
  process.exit(2)
}

if (build.status !== 0) {
  process.exit(build.status ?? 2)
}

const output = await import('./dist/index.mjs')
const styleExports = Object.values(output).filter(
  (value) => value !== null && typeof value === 'object',
)

if (styleExports.length < 2) {
  console.error(`Expected at least two style exports, got ${styleExports.length}`)
  process.exit(2)
}

const css = readFileSync('dist/style.css', 'utf8')
const duplicateCount = css.match(/cursor: pointer/g)?.length ?? 0

if (duplicateCount < 2) {
  console.log(`CSS composition count: ${duplicateCount}`)
  process.exit(0)
}

console.error(`DUPLICATED_CSS_MODULE_COMPOSITION count=${duplicateCount}`)
process.exit(23)
