-- 테이블 정의. 새로 DB 를 만들 때 이 파일을 그대로 실행하면 된다.
--   mysql -u root -p itoy < server/schema.sql
--
-- 순서가 중요하다 — friends/goal_tags/meals 가 users 를 FK 로 참조하고,
-- meals 가 friends 를 FK 로 참조한다.

CREATE TABLE IF NOT EXISTS users (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    account       VARCHAR(50)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    email         VARCHAR(255) UNIQUE,
    name          VARCHAR(30),
    age           INT,
    keyword       VARCHAR(30),
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS friends (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    user_id    INT NOT NULL,
    name       VARCHAR(50)  NOT NULL,
    image_url  VARCHAR(500) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_friends_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS goal_tags (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    user_id    INT NOT NULL,
    content    VARCHAR(100) NOT NULL,
    use_count  INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_goal_tags_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    UNIQUE KEY uk_goal_tags_user_content (user_id, content)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS meals (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    friend_id   INT DEFAULT NULL,
    mission_no  INT NOT NULL,
    goals       JSON NOT NULL,
    started_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at    TIMESTAMP NULL DEFAULT NULL,
    CONSTRAINT fk_meals_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_meals_friend FOREIGN KEY (friend_id) REFERENCES friends (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- WS /doll/talk 세션 1건 = 1행. transcript 는 turn_complete 마다 확정된
-- 인형 발화(turn)를 담은 JSON 배열(["...", "..."])이다 — 아이 쪽 발화는
-- 텍스트로 전사되지 않으므로(README 프레임 규약 참조) 여기 포함되지 않는다.
-- 연결이 끊기면(정상 종료든 오류든) ended_at 과 그때까지의 transcript 로 갱신한다.
CREATE TABLE IF NOT EXISTS talk_sessions (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    transcript  JSON NOT NULL,
    started_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at    TIMESTAMP NULL DEFAULT NULL,
    CONSTRAINT fk_talk_sessions_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
