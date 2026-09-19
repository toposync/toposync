const { defineConfig } = require("@playwright/test");
const source = require("./playwright.source-panorama.config");
module.exports = defineConfig({ ...source, testMatch: "navigable-viewport.spec.js", outputDir: ".toposync-data/viewport-validation/test-results", reporter: [["list"]] });
