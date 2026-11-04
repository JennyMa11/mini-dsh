const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert');
const context = {};
Object.defineProperty(context, 'console', {value: Object.freeze({assert: (condition) => assert.ok(condition), log: () => {}}), writable: false});
vm.createContext(context);
vm.runInContext(fs.readFileSync('solution.js', 'utf8'), context, {timeout: 5000});
vm.runInContext("const testRemoveDuplicates = () => {\n  console.assert(JSON.stringify(removeDuplicates([])) === JSON.stringify([]))\n  console.assert(\n    JSON.stringify(removeDuplicates([1, 2, 3, 4])) ===\n      JSON.stringify([1, 2, 3, 4])\n  )\n  console.assert(\n    JSON.stringify(removeDuplicates([1, 2, 3, 2, 4, 3, 5])) ===\n      JSON.stringify([1, 4, 5])\n  )\n}\n\ntestRemoveDuplicates()\n", context, {timeout: 5000});
process.stdout.write("independent_passed\n");
