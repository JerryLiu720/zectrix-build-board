//! 只负责推图与按键的本地程序（不做自动重连）。
//! 用法：build-board-pusher <IP 或设备 ID> <PNG 路径>
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use zectrix_sdk::{Device, Event, ImageFormat, Key, KeyAction, OpenOptions};

/// FNV-1a：判断画面是否真的变了
fn fingerprint(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn read_u32(path: &Path, default: u32) -> u32 {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(default)
}

async fn push(device: &Device, png: &Path) -> Option<u64> {
    let bytes = match fs::read(png) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("读取图片失败：{e}");
            return None;
        }
    };
    match device.show_with_format(&bytes, ImageFormat::Gray16).await {
        Ok(_) => {
            println!("已推送 {} 字节", bytes.len());
            Some(fingerprint(&bytes))
        }
        Err(e) => {
            // ResultUnknown 等情况不重推，交由下一次内容变化时自然重试
            eprintln!("推送失败（不重试）：{e}");
            None
        }
    }
}

/// 等渲染器写出新画面；超时返回当前指纹
async fn wait_change(png: &Path, old: u64, timeout: Duration) -> u64 {
    let start = Instant::now();
    while start.elapsed() < timeout {
        tokio::time::sleep(Duration::from_millis(200)).await;
        if let Ok(b) = fs::read(png) {
            let fp = fingerprint(&b);
            if fp != old {
                return fp;
            }
        }
    }
    old
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("用法：build-board-pusher <IP 或设备 ID> <PNG 路径>");
        std::process::exit(1);
    }
    let target = args[1].clone();
    let png = PathBuf::from(&args[2]);
    let dir = png.parent().map(Path::to_path_buf).unwrap_or_else(|| PathBuf::from("."));

    let pages = read_u32(&dir.join("pages.txt"), 2).max(1);
    let state = dir.join("ui_state.txt");
    let force = dir.join("force.req");
    let mut page = read_u32(&state, 0) % pages;

    // 连接一次；失败就退出，不自动重连、不抢占
    let device = match Device::open(OpenOptions {
        target: Some(target.clone()),
        client_name: Some("Zectrix Build Board".into()),
    })
    .await
    {
        Ok(d) => d,
        Err(e) => {
            let msg = format!("{e}");
            if msg.contains("Busy") || msg.contains("占用") || msg.contains("忙") {
                eprintln!("设备忙（Busy）：已有另一个本地推送进程在占用。请先停止它，本程序不会抢占。");
            } else if msg.contains("NoDevice") || msg.contains("没有发现") {
                eprintln!("未找到设备：确认与电脑同一 Wi-Fi，并在设备上开启「待机保持 Wi-Fi」。");
            } else {
                eprintln!("连接失败：{msg}");
            }
            std::process::exit(2);
        }
    };
    println!("已连接 {target}，共 {pages} 页");

    // 心跳：渲染器据此显示「已连接」
    {
        let hb = dir.join("conn.txt");
        tokio::spawn(async move {
            loop {
                let _ = fs::write(&hb, now().to_string());
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
        });
    }

    let mut last_fp = push(&device, &png).await.unwrap_or(0);

    loop {
        match device.next_event().await {
            Ok(Some(Event::Key { key, action })) => {
                let mut pending = false;
                match (action, key) {
                    (KeyAction::Short, Key::Up) => {
                        page = (page + pages - 1) % pages;
                        let _ = fs::write(&state, page.to_string());
                        pending = true;
                    }
                    (KeyAction::Short, Key::Down) => {
                        page = (page + 1) % pages;
                        let _ = fs::write(&state, page.to_string());
                        pending = true;
                    }
                    (KeyAction::Short, Key::Confirm) | (KeyAction::Long, Key::Confirm) => {
                        let _ = fs::write(&force, "1");
                        pending = true;
                    }
                    _ => {}
                }
                if pending {
                    // 翻页要立刻出图；确认键要等重新拉取，给更长时间
                    let wait = if key == Key::Confirm {
                        Duration::from_secs(15)
                    } else {
                        Duration::from_secs(6)
                    };
                    let fp = wait_change(&png, last_fp, wait).await;
                    if fp != last_fp {
                        last_fp = push(&device, &png).await.unwrap_or(fp);
                        println!("按键 {:?} → 第 {} 页", key, page + 1);
                    } else {
                        eprintln!("按键后没有新画面（超时）");
                    }
                }
            }
            Ok(Some(Event::Disconnected { .. })) => {
                eprintln!("设备已断开，退出（不自动重连）");
                break;
            }
            Ok(None) => {
                eprintln!("事件流结束，退出");
                break;
            }
            Ok(Some(_)) => {}
            Err(e) => {
                eprintln!("读取事件失败：{e}");
                break;
            }
        }
    }

    let _ = device.close().await;
}
