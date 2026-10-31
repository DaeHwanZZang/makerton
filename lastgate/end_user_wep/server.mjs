import { createLockerClient } from './src/locker-client.mjs';
import { createServer } from './src/api-server.mjs';

const port = Number(process.env.PORT || 4180);
const host = process.env.HOST || '127.0.0.1';
const brokerUrl = process.env.LOCKER_BROKER_URL ||
  `mqtt://${process.env.LOCKER_BROKER_HOST || 'localhost'}:${process.env.LOCKER_BROKER_PORT || 1883}`;

const locker = createLockerClient({
  url: brokerUrl,
  onStatus: ok => console.log(ok ? `[MQTT] 보관소 브로커 연결됨 (${brokerUrl})` : '[MQTT] 브로커 연결 끊김 — 자동 재시도')
});
const server = createServer({ locker });

server.on('error', error => {
  if (error.code === 'EADDRINUSE') {
    console.error(`\n[실행 중] ${host}:${port} 포트를 이미 다른 서버가 사용하고 있습니다.`);
    console.error('기존 LastGate 세대 웹이 열려 있다면 새로 실행할 필요가 없습니다.');
    console.error(`확인 주소: http://${host}:${port}/\n`);
  } else {
    console.error('\n서버를 시작하지 못했습니다:', error.message);
  }
  locker.close();
  process.exitCode = 1;
});

server.listen(port, host, () => {
  console.log(`\n세대 웹: http://${host}:${port}/`);
  console.log(`서버 상태 확인: http://${host}:${port}/api/health`);
  console.log(`보관소 브로커: ${brokerUrl}\n`);
});

let shuttingDown = false;
function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log('\nLastGate 세대 웹을 종료합니다.');
  const done = async code => {
    await locker.close();
    process.exit(code);
  };
  if (!server.listening) return done(0);
  server.close(error => done(error ? 1 : 0));
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
