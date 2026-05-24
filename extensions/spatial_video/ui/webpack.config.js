const path = require("path");
const { container } = require("webpack");
const { createSharedDeps } = require("../../../frontend/moduleFederationShared");

/** @type {import("webpack").Configuration} */
module.exports = {
  entry: path.resolve(__dirname, "src", "entry.ts"),
  output: {
    path: path.resolve(__dirname, "..", "src", "toposync_ext_spatial_video", "static"),
    publicPath: "auto",
    filename: "[name].js",
    chunkFilename: "[name].js",
    clean: true
  },
  resolve: {
    extensions: [".ts", ".tsx", ".js"]
  },
  module: {
    rules: [
      {
        test: /\.tsx?$/,
        loader: "ts-loader",
        options: { transpileOnly: true },
        exclude: /node_modules/
      }
    ]
  },
  plugins: [
    new container.ModuleFederationPlugin({
      name: "spatial_video",
      filename: "remoteEntry.js",
      exposes: {
        "./activate": "./src/activate.tsx"
      },
      shared: createSharedDeps({ includeThree: true })
    })
  ],
  optimization: {
    splitChunks: false,
    runtimeChunk: false
  }
};
