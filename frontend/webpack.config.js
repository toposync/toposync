const path = require("path");
const HtmlWebpackPlugin = require("html-webpack-plugin");
const { container } = require("webpack");

function envInt(name, fallback) {
  const raw = String(process.env[name] ?? "").trim();
  if (!raw) return fallback;
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function envText(name, fallback = "") {
  const raw = String(process.env[name] ?? "").trim();
  return raw || fallback;
}

const frontendPort = envInt("TOPOSYNC_FRONTEND_PORT", 5173);
const backendHost = envText("TOPOSYNC_BACKEND_HOST", "127.0.0.1");
const backendPort = envInt("TOPOSYNC_BACKEND_PORT", 8000);
const backendTarget = envText("TOPOSYNC_BACKEND_TARGET", `http://${backendHost}:${backendPort}`);

/** @type {import("webpack").Configuration} */
module.exports = {
  entry: path.resolve(__dirname, "src", "index.tsx"),
  output: {
    path: path.resolve(__dirname, "dist"),
    publicPath: "/",
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
      },
      {
        test: /\.css$/,
        use: ["style-loader", "css-loader"]
      },
      {
        test: /\.(woff2?|eot|ttf|otf|svg)$/i,
        type: "asset/resource"
      }
    ]
  },
  plugins: [
    new HtmlWebpackPlugin({
      template: path.resolve(__dirname, "src", "index.html")
    }),
    new container.ModuleFederationPlugin({
      name: "toposync_host",
      remotes: {},
      shared: {
        react: { singleton: true, requiredVersion: false },
        "react-dom": { singleton: true, requiredVersion: false },
        three: { singleton: true, requiredVersion: false }
      }
    })
  ],
  devtool: "source-map",
  devServer: {
    port: frontendPort,
    historyApiFallback: true,
    proxy: [
      {
        context: ["/api", "/extensions", "/files"],
        target: backendTarget,
        changeOrigin: true
      }
    ]
  }
};
