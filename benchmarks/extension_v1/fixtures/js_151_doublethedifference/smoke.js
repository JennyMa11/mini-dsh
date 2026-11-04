const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert');
const context = {};
Object.defineProperty(context, 'console', {value: Object.freeze({assert: (condition) => assert.ok(condition), log: () => {}}), writable: false});
vm.createContext(context);
vm.runInContext(fs.readFileSync('solution.js', 'utf8'), context, {timeout: 5000});
vm.runInContext("const testDoubleTheDifference = () => {\n  console.assert(doubleTheDifference([1,3,2,0]) === 10)\n  console.assert(doubleTheDifference([-1,-2,0]) === 0)\n  console.assert(doubleTheDifference([9,-2]) === 81)\n  console.assert(doubleTheDifference([0]) === 0)\n}\ntestDoubleTheDifference()\n", context, {timeout: 5000});
process.stdout.write("examples_passed\n");
