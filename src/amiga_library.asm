; Library prefix for the generated Amiga depackers.
; Assemble with: vasmm68k_mot -m68000 -Fbin -no-opt -o prefix.bin amiga_library.asm
; The bytes are embedded in bootstrap.cpp; normal builds need no assembler.
;
; RAMLIB calls this entry when no Resident tag is visible in the packed file.
; A0 is the original segment-list BPTR. Refuse ordinary CLI execution.
entry:
        lea     entry-4(pc),a1
        move.l  a0,d0
        lsl.l   #2,d0
        cmpa.l  d0,a1
        beq.s   initialize
        moveq   #-1,d0
        rts
initialize:
        movem.l d2-d7/a2-a6,-(sp)
        move.l  a0,-(sp)
        bra.w   depacker
; The depacker leaves second and first payload addresses on the stack.
; Save() patches the stack displacement (0 or 4) and merged Resident offset.
finish:
        movea.l 4(sp),a1
        adda.l  #$12345678,a1
        addq.l  #8,sp
        move.l  (sp)+,d1
        movea.l 4.w,a6
        jsr     -102(a6)       ; InitResident(A1=Resident, D1=segList)
        movem.l (sp)+,d2-d7/a2-a6
        rts
depacker:
