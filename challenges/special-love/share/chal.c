#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <stdint.h>

void win(){
    system("/bin/sh");
}

__attribute__((constructor)) void init_program(){
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stdin, NULL, _IONBF, 0);
    int lol = 67;
    puts("Special gifts for a special you:");
    printf("init_program: %p\n", init_program);
    printf("puts: %p\n", puts);
    printf("lol: %p\n", &lol);
}

int main(){
    puts("An special arbitrary write for a special you!!!");
    uint64_t *target = 0;
    uint64_t value = 0;
    scanf("%p %zx", &target, &value);
    *target = value;
    printf("write %lx -> %p\n", value, target);
    _exit(0);
}
