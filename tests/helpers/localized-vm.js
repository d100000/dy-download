const fs = require('node:fs');
const vm = require('node:vm');
const ui = 'globalThis.__UI_MESSAGES = ' + fs.readFileSync('static/ui-locales.json', 'utf8') + ';\n'
  + fs.readFileSync('static/ui-i18n.js', 'utf8');
module.exports = {...vm, runInNewContext(source, context = {}, options) {
  context.LANG ??= 'zh';
  return vm.runInNewContext(ui + '\n' + source, context, options);
}};
