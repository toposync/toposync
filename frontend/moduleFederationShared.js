const pluginApiPackage = require("./packages/plugin-api/package.json");

function createSharedDeps(options = {}) {
  const includeThree = Boolean(options.includeThree);

  return {
    "@toposync/plugin-api": {
      singleton: true,
      requiredVersion: `^${pluginApiPackage.version}`
    },
    react: {
      singleton: true,
      requiredVersion: false
    },
    "react-dom": {
      singleton: true,
      requiredVersion: false
    },
    ...(includeThree
      ? {
          three: {
            singleton: true,
            requiredVersion: false
          }
        }
      : {})
  };
}

module.exports = {
  createSharedDeps,
  pluginApiVersion: pluginApiPackage.version
};
