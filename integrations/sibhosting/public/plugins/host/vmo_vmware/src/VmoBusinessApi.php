<?php
/** Only VM Orchestrator's restricted /api/business/* API is used. */
class VmoBusinessApiException extends \RuntimeException {}

class VmoBusinessApi
{
    public function request(array $settings, string $method, string $path, ?array $payload = null): array
    {
        $base = $this->validatedOrigin((string)($settings['vmo_api_url'] ?? ''), true);
        $key = (string)($settings['vmo_business_key'] ?? '');
        if (strlen($key) < 32) {
            throw new VmoBusinessApiException('VMO 业务密钥未正确配置（至少 32 字符）');
        }
        // No paths outside the VMO *business* API are allowed from this plugin.
        if (!preg_match('#^/api/business/(?:health|services(?:/[A-Za-z0-9_.:%-]+(?:/(?:state|ticket|expiry))?)?)$#D', $path)) {
            throw new VmoBusinessApiException('不允许访问非业务 API 路径');
        }
        if (!in_array($method, ['GET', 'POST', 'PUT'], true)) {
            throw new VmoBusinessApiException('HTTP 操作不被允许');
        }
        if (!function_exists('curl_init')) {
            throw new VmoBusinessApiException('PHP 缺少 cURL 扩展');
        }
        $ch = curl_init($base . $path);
        if ($ch === false) throw new VmoBusinessApiException('初始化网络连接失败');
        $headers = [
            'X-VMO-Business-Key: ' . $key,
            'Accept: application/json',
            'Content-Type: application/json',
            'User-Agent: SibHosting-VMO/1.3.3',
        ];
        $options = [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_CONNECTTIMEOUT => 5,
            CURLOPT_TIMEOUT => 25,
            CURLOPT_MAXREDIRS => 0,
            CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
        ];
        if ($payload !== null) {
            $options[CURLOPT_POSTFIELDS] = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR);
        }
        curl_setopt_array($ch, $options);
        $result = curl_exec($ch);
        $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $errNo = curl_errno($ch);
        curl_close($ch);
        if ($result === false) {
            throw new VmoBusinessApiException('VMO 业务接口连接失败（cURL ' . $errNo . '，请检查 HTTPS 和网络）');
        }
        if ($status < 200 || $status >= 300) {
            // No upstream body: it might contain internal topology or secrets.
            $reason = [401=>'业务密钥错误',403=>'权限拒绝',404=>'服务或映射不存在',
                       409=>'资源或归属冲突',422=>'请求参数不匹配',503=>'业务服务未就绪'];
            throw new VmoBusinessApiException('VMO 请求失败：HTTP ' . $status . ' ' . ($reason[$status] ?? '请查看 VMO 后台日志'));
        }
        if (strlen($result) > 1024 * 1024) {
            throw new VmoBusinessApiException('VMO 返回内容过大');
        }
        $response = json_decode($result, true);
        if (!is_array($response)) {
            throw new VmoBusinessApiException('VMO 返回的不是 JSON 对象');
        }
        return $response;
    }

    public function validatedOrigin(string $origin, bool $allowLoopbackHttp): string
    {
        $origin = rtrim(trim($origin), '/');
        $u = parse_url($origin);
        if (!$u || !isset($u['scheme'], $u['host']) || isset($u['user']) || isset($u['pass']) ||
            isset($u['query']) || isset($u['fragment']) || (isset($u['path']) && !in_array($u['path'], ['', '/'], true))) {
            throw new VmoBusinessApiException('URL 必须是完整的站点根地址，不允许路径、参数或账号密码');
        }
        $scheme = strtolower((string)$u['scheme']);
        $hostname = strtolower((string)$u['host']);
        $loopback = in_array($hostname, ['localhost','127.0.0.1','::1','[::1]'], true);
        if ($scheme !== 'https' && !($allowLoopbackHttp && $loopback && $scheme === 'http')) {
            throw new VmoBusinessApiException('远程连接必须使用 HTTPS；本机回环地址可使用 HTTP');
        }
        return $origin;
    }
}
