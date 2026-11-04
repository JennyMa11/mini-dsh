const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert');
const context = {};
Object.defineProperty(context, 'console', {value: Object.freeze({assert: (condition) => assert.ok(condition), log: () => {}}), writable: false});
vm.createContext(context);
vm.runInContext(fs.readFileSync('solution.js', 'utf8'), context, {timeout: 5000});
vm.runInContext("const testDoubleTheDifference = () => {\n  console.assert(doubleTheDifference([]) === 0)\n  console.assert(doubleTheDifference([5, 4]) === 25)\n  console.assert(doubleTheDifference([0.1, 0.2, 0.3]) === 0)\n  console.assert(doubleTheDifference([-10, -20, -30]) === 0)\n  console.assert(doubleTheDifference([-1, -2, 8]) === 0)\n  console.assert(doubleTheDifference([0.2, 3, 5]) === 34)\n  let lst = []\n  let odd_sum = 0\n  for (let i = -99; i < 100; i += 2) {\n    if (i % 2 != 0 && i > 0) { odd_sum += i * i }\n    lst.push(i)\n  }\n  console.assert(doubleTheDifference(lst) === odd_sum)\n}\ntestDoubleTheDifference()\n", context, {timeout: 5000});
process.stdout.write("independent_passed\n");
