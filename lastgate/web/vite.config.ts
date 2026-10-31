import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// host: true → 같은 네트워크의 키오스크 단말에서도 접속 가능
export default defineConfig({
  plugins: [react()],
  server: { port: 4173, host: true },
  preview: { port: 4174, host: true },
});
