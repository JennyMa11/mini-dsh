const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert');
const context = {};
Object.defineProperty(context, 'console', {value: Object.freeze({assert: (condition) => assert.ok(condition), log: () => {}}), writable: false});
vm.createContext(context);
vm.runInContext(fs.readFileSync('solution.js', 'utf8'), context, {timeout: 5000});
vm.runInContext("const testRemoveDuplicates = () => {\n  console.assert(\n    JSON.stringify(removeDuplicates([1, 2, 3, 2,4])) ===\n      JSON.stringify([1,3, 4])\n  )\n}\ntestRemoveDuplicates()\n", context, {timeout: 5000});
process.stdout.write("examples_passed\n");
