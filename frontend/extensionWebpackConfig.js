const path = require("path");
const { container } = require("webpack");
const { createSharedDeps } = require("./moduleFederationShared");

function readPackageJson(uiDir) {
  return require(path.resolve(uiDir, "package.json"));
}

module.exports = (_env, argv = {}) => {
  const uiDir = process.cwd();
  const extensionDir = path.resolve(uiDir, "..");
  const extensionName = path.basename(extensionDir);
  const packageJson = readPackageJson(uiDir);
  const dependencies = packageJson.dependencies || {};
  const devtool = argv.mode === "development" ? "source-map" : false;

  return {
    entry: path.resolve(uiDir, "src", "entry.ts"),
    output: {
      path: path.resolve(extensionDir, "src", `toposync_ext_${extensionName}`, "static"),
      publicPath: "auto",
      filename: "[name].js",
      chunkFilename: "[name].js",
      clean: true,
    },
    devtool,
    resolve: {
      extensions: [".ts", ".tsx", ".js"],
    },
    module: {
      rules: [
        {
          test: /\.tsx?$/,
          loader: "ts-loader",
          options: { transpileOnly: true },
          exclude: /node_modules/,
        },
        {
          test: /\.svg$/i,
          type: "asset/source",
        },
      ],
    },
    plugins: [
      new container.ModuleFederationPlugin({
        name: extensionName,
        filename: "remoteEntry.js",
        exposes: {
          "./activate": "./src/activate.tsx",
        },
        shared: createSharedDeps({ includeThree: Boolean(dependencies.three) }),
      }),
    ],
    optimization: {
      splitChunks: false,
      runtimeChunk: false,
    },
  };
};
