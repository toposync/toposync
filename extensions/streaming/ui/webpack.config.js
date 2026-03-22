const path = require("path");
const { container } = require("webpack");

/** @type {import("webpack").Configuration} */
module.exports = {
  entry: path.resolve(__dirname, "src", "entry.ts"),
  output: {
    path: path.resolve(__dirname, "..", "src", "toposync_ext_streaming", "static"),
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
      name: "streaming",
      filename: "remoteEntry.js",
      exposes: {
        "./activate": "./src/activate.tsx"
      },
      shared: {
        react: { singleton: true, requiredVersion: false },
        "react-dom": { singleton: true, requiredVersion: false },
        three: { singleton: true, requiredVersion: false }
      }
    })
  ],
  optimization: {
    splitChunks: false,
    runtimeChunk: false
  }
};
