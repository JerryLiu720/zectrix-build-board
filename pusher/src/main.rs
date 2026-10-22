//! 只负责推图与按键的本地程序（不做自动重连）。
//! 持续监听 PNG 变化：翻页、刷新、定时更新都会自动上屏。
//! 用法：build-board-pusher <IP 或设备 ID> <PNG 路径>
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
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

/// 推送一帧；失败不重试（ResultUnknown 不等于失败），等下次内容变化自然再推
async fn push(device: &Device, bytes: Vec<u8>) -> Option<u64> {
    if bytes.is_empty() {
        return None;
    }
    match device.show_with_format(&bytes, ImageFormat::Gray16).await {
        Ok(_) => {
            println!("已推送 {} 字节", bytes.len());
            Some(fingerprint(&bytes))
        }
        Err(e) => {
            eprintln!("推送失败（不重试）：{e}");
            None
        }
    }
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

    // 首推
    let mut last_fp = match fs::read(&png) {
        Ok(bytes) => push(&device, bytes).await.unwrap_or(0),
        Err(_) => 0,
    };

    // 持续监听文件变化：按键、刷新回执、定时更新都会走到这里
    let mut ticker = tokio::time::interval(Duration::from_millis(600));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            ev = device.next_event() => {
                match ev {
                    Ok(Some(Event::Key { key, action })) => {
                        match (action, key) {
                            (KeyAction::Short, Key::Up) => {
                                page = (page + pages - 1) % pages;
                                let _ = fs::write(&state, page.to_string());
                            }
                            (KeyAction::Short, Key::Down) => {
                                page = (page + 1) % pages;
                                let _ = fs::write(&state, page.to_string());
                            }
                            (KeyAction::Short, Key::Confirm) | (KeyAction::Long, Key::Confirm) => {
                                let _ = fs::write(&force, "1");
                            }
                            _ => {}
                        }
                        println!("按键 {:?} → 第 {} 页", key, page + 1);
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
            _ = ticker.tick() => {
                if let Ok(bytes) = fs::read(&png) {
                    if fingerprint(&bytes) != last_fp {
                        if let Some(fp) = push(&device, bytes).await {
                            last_fp = fp;
                        }
                    }
                }
            }
        }
    }

    let _ = device.close().await;
}
