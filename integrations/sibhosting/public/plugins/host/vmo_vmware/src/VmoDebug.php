<?php
/** Safe opt-in diagnostics. Never record credentials, session tickets or request payloads. */
final class VmoDebug
{
    public const VERSION = '1.3.3';

    public static function modeFromParams($params): string
    {
        if (!is_array($params)) return '0';
        if (isset($params['params']) && is_array($params['params'])) $params = $params['params'];
        $meta = $params['server_meta'] ?? [];
        // Legacy probe values 2/3 now behave as normal mode, not test pages.
        return is_array($meta) && (string)($meta['vmo_debug_mode'] ?? '0') === '1' ? '1' : '0';
    }

    public static function describeResult($result): array
    {
        $summary = ['type'=>gettype($result), 'is_array'=>is_array($result)];
        if (!is_array($result)) return $summary;
        $summary['keys'] = self::safeKeys($result);
        $summary['field_types'] = [];
        foreach ($result as $key => $value) {
            if (is_string($key)) $summary['field_types'][$key] = gettype($value);
        }
        if (isset($result['vars']) && is_array($result['vars'])) {
            $summary['vars_keys'] = self::safeKeys($result['vars']);
        }
        return $summary;
    }

    public static function describeException(\Throwable $e): array
    {
        return ['class'=>get_class($e),
            'message_digest'=>substr(hash('sha256', $e->getMessage()), 0, 12)];
    }

    public static function record(string $mode, string $event, array $details = []): void
    {
        if ($mode !== '1') return;
        // Only descriptions from describeResult/describeException or hardcoded field types.
        $entry = [
            'at'=>gmdate('Y-m-d\TH:i:s\Z'), 'plugin'=>'vmo_vmware',
            'version'=>self::VERSION, 'event'=>preg_replace('/[^a-zA-Z0-9_.-]/', '', $event),
            'details'=>$details,
        ];
        $line = json_encode($entry, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR);
        if (!is_string($line)) return;
        try {
            $folder = dirname(__DIR__, 5) . '/runtime/log';
            if (!is_dir($folder) && !@mkdir($folder, 0700, true) && !is_dir($folder)) return;
            $path = $folder . '/vmo_vmware_debug-' . gmdate('Ymd') . '.log';
            if (is_file($path) && @filesize($path) >= 2 * 1024 * 1024) return;
            if (@file_put_contents($path, $line . "\n", FILE_APPEND | LOCK_EX) !== false) @chmod($path, 0600);
        } catch (\Throwable $ignored) {
            // Logging errors must not affect billing or customer access.
        }
    }

    private static function safeKeys(array $data): array
    {
        $keys = [];
        foreach (array_slice(array_keys($data), 0, 30) as $key) {
            $key = (string)$key;
            $keys[] = preg_match('/^[a-zA-Z0-9_:-]{1,60}$/D', $key) ? $key : '[other-key]';
        }
        return $keys;
    }
}
