import react from "@vitejs/plugin-react";

export default {
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8123",
    },
  },
};
